data "nebius_iam_v1_project" "project" {
  id = var.iam_project_id
}

data "nebius_vpc_v1_subnet" "subnet" {
  id = var.vpc_subnet_id
}


resource "nebius_mk8s_v1_cluster" "cluster" {
  parent_id = data.nebius_iam_v1_project.project.id
  name      = "${local.name_prefix}-${terraform.workspace}"
  control_plane = {
    version   = "1.31"
    subnet_id = data.nebius_vpc_v1_subnet.subnet.id
    endpoints = {
      public_endpoint = {}
    }
  }
}

resource "nebius_mk8s_v1_node_group" "system-ng" {
  name             = "${nebius_mk8s_v1_cluster.cluster.name}-system-ng"
  parent_id        = nebius_mk8s_v1_cluster.cluster.id
  fixed_node_count = 2
  metadata = {
  }
  template = {
    boot_disk = {
      block_size_bytes = 4096
      size_gibibytes   = 96
      type             = "NETWORK_SSD"
    }
    network_interfaces = [
      {
        subnet_id = data.nebius_vpc_v1_subnet.subnet.id
      },
    ]
    resources = {
      platform = "cpu-d3"
      preset   = "4vcpu-16gb"
    }
    service_account_id = nebius_iam_v1_service_account.node-sa.id
    taints = [{
      key    = "CriticalAddonsOnly",
      value  = "true",
      effect = "NO_SCHEDULE"
    }]
  }
  version = nebius_mk8s_v1_cluster.cluster.control_plane.version
}
