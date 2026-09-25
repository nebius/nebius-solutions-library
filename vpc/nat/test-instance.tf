# This instance is deployed only for testing purposes.
# You can remove this configuration, if there is no need for deploying a test instance to the private subnet.

resource "nebius_compute_v1_disk" "gw_test_boot_disk" {
  parent_id           = var.parent_id
  name                = "gw-test-boot-disk"
  block_size_bytes    = 4096
  size_bytes          = 50 * 1024 * 1024 * 1024 # 60 GiB
  type                = "NETWORK_SSD"
  source_image_family = { image_family = "ubuntu24.04-driverless" }
}

resource "nebius_compute_v1_instance" "gw_test_instance" {
  parent_id = var.parent_id
  name = "${var.vpc_name}-gateway-test-instance"

  boot_disk = {
    attach_mode   = "READ_WRITE"
    existing_disk = nebius_compute_v1_disk.gw_test_boot_disk
  }

  network_interfaces = [
    {
      name  = "eth0"
      subnet_id = nebius_vpc_v1_subnet.private_subnet.id
      ip_address  = {}
    }
  ]

  resources = {
    platform  = "cpu-d3"
    preset    = "4vcpu-16gb"
  }

  cloud_init_user_data = templatefile("../../modules/cloud-init/gateway-test-cloud-init.tftpl", {
    ssh_user_name      = var.ssh_user_name
    ssh_public_key     = local.ssh_public_key
  })
}