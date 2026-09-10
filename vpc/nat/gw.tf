resource "nebius_compute_v1_disk" "gateway_boot_disk" {
  parent_id           = var.parent_id
  name                = "gw-boot-disk"
  block_size_bytes    = 4096
  size_bytes          = 50 * 1024 * 1024 * 1024 # 60 GiB
  type                = "NETWORK_SSD"
  source_image_family = { image_family = "ubuntu24.04-driverless" }
}

resource "nebius_compute_v1_instance" "gw_instance" {
  parent_id = var.parent_id
  name = "${var.vpc_name}-gateway-instance"

  boot_disk = {
    attach_mode   = "READ_WRITE"
    existing_disk = nebius_compute_v1_disk.gateway_boot_disk
  }

  network_interfaces = [
    {
      name  = "eth0"
      subnet_id = nebius_vpc_v1_subnet.gateway_subnet.id
      ip_address  = { allocation_id = nebius_vpc_v1_allocation.gateway_allocation.id }
      public_ip_address = {}
    }
  ]

  resources = {
    platform  = "cpu-d3"
    preset    = "4vcpu-16gb"
  }

  cloud_init_user_data = templatefile("../../modules/cloud-init/gateway-cloud-init.tftpl", {
    ssh_user_name      = var.ssh_user_name
    ssh_public_key     = local.ssh_public_key
  })
}