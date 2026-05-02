data "external" "existing_network" {
  program = ["bash", "${path.module}/scripts/validate-network.sh"]

  query = {
    network_id = var.vpc_network_id
    parent_id  = var.parent_id
  }
}

resource "terraform_data" "network_validation" {
  input = data.external.existing_network.result

  lifecycle {
    precondition {
      condition     = data.external.existing_network.result.network_exists == "true"
      error_message = "VPC network ${var.vpc_network_id} was not found or could not be read with the current Nebius CLI credentials. ${data.external.existing_network.result.error}"
    }

    precondition {
      condition     = data.external.existing_network.result.project_matches == "true"
      error_message = "VPC network ${var.vpc_network_id} does not belong to project ${var.parent_id}."
    }
  }
}

resource "nebius_vpc_v1_subnet" "gateway_subnet" {
  parent_id  = var.parent_id
  network_id = var.vpc_network_id
  name       = "${var.name_prefix}-gateway-subnet"

  depends_on = [terraform_data.network_validation]
}

resource "nebius_vpc_v1_route_table" "nat_gateway_table" {
  parent_id  = var.parent_id
  network_id = var.vpc_network_id
  name       = "${var.name_prefix}-route-table"

  depends_on = [terraform_data.network_validation]
}

resource "nebius_vpc_v1_subnet" "private_subnet" {
  parent_id      = var.parent_id
  network_id     = var.vpc_network_id
  route_table_id = nebius_vpc_v1_route_table.nat_gateway_table.id
  name           = "${var.name_prefix}-private-subnet"

  depends_on = [terraform_data.network_validation]
}

resource "nebius_vpc_v1_allocation" "gateway_private_ip" {
  parent_id = var.parent_id
  name      = "${var.name_prefix}-gateway-private-ip"

  ipv4_private = {
    cidr      = "/32"
    subnet_id = nebius_vpc_v1_subnet.gateway_subnet.id
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "nebius_vpc_v1_allocation" "gateway_public_ip" {
  parent_id = var.parent_id
  name      = "${var.name_prefix}-gateway-public-ip"

  ipv4_public = {
    cidr      = "/32"
    subnet_id = nebius_vpc_v1_subnet.gateway_subnet.id
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "nebius_vpc_v1_allocation" "workload_private_ip" {
  count     = var.deploy_test_vm ? 1 : 0
  parent_id = var.parent_id
  name      = "${var.name_prefix}-workload-private-ip"

  ipv4_private = {
    cidr      = "/32"
    subnet_id = nebius_vpc_v1_subnet.private_subnet.id
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "nebius_compute_v1_instance" "gateway_vm" {
  parent_id = var.parent_id
  name      = "${var.name_prefix}-gateway-vm"

  boot_disk = {
    attach_mode   = "READ_WRITE"
    existing_disk = nebius_compute_v1_disk.gateway_boot_disk
  }

  network_interfaces = [
    {
      name      = "eth0"
      subnet_id = nebius_vpc_v1_subnet.gateway_subnet.id
      ip_address = {
        allocation_id = nebius_vpc_v1_allocation.gateway_private_ip.id
      }
      public_ip_address = {
        allocation_id = nebius_vpc_v1_allocation.gateway_public_ip.id
      }
    }
  ]

  resources = {
    platform = var.gateway_platform
    preset   = var.gateway_preset
  }

  cloud_init_user_data = templatefile("../modules/cloud-init/nat-gateway-cloud-init.tftpl", {
    ssh_user_name  = var.ssh_user_name
    ssh_public_key = local.ssh_public_key
  })
}

resource "nebius_vpc_v1_route" "default_to_gateway" {
  parent_id = nebius_vpc_v1_route_table.nat_gateway_table.id
  name      = "${var.name_prefix}-default-route"

  destination = {
    cidr = "0.0.0.0/0"
  }

  next_hop = {
    allocation = {
      id = nebius_vpc_v1_allocation.gateway_private_ip.id
    }
  }
}

resource "nebius_compute_v1_instance" "workload_vm" {
  count     = var.deploy_test_vm ? 1 : 0
  parent_id = var.parent_id
  name      = "${var.name_prefix}-workload-vm"

  boot_disk = {
    attach_mode   = "READ_WRITE"
    existing_disk = nebius_compute_v1_disk.workload_boot_disk[0]
  }

  network_interfaces = [
    {
      name      = "eth0"
      subnet_id = nebius_vpc_v1_subnet.private_subnet.id
      ip_address = {
        allocation_id = nebius_vpc_v1_allocation.workload_private_ip[0].id
      }
    }
  ]

  resources = {
    platform = var.workload_platform
    preset   = var.workload_preset
  }

  cloud_init_user_data = templatefile("../modules/cloud-init/private-vm-cloud-init.tftpl", {
    ssh_user_name  = var.ssh_user_name
    ssh_public_key = local.ssh_public_key
  })
}
