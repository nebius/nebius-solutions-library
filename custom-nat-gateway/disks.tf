resource "nebius_compute_v1_disk" "gateway_boot_disk" {
  parent_id           = var.parent_id
  name                = "${var.name_prefix}-gateway-boot-disk"
  size_bytes          = var.gateway_boot_disk_size_gib * 1024 * 1024 * 1024
  block_size_bytes    = 4096
  type                = "NETWORK_SSD"
  source_image_family = { image_family = var.source_image_family }
}

resource "nebius_compute_v1_disk" "workload_boot_disk" {
  count               = var.deploy_test_vm ? 1 : 0
  parent_id           = var.parent_id
  name                = "${var.name_prefix}-workload-boot-disk"
  size_bytes          = var.workload_boot_disk_size_gib * 1024 * 1024 * 1024
  block_size_bytes    = 4096
  type                = "NETWORK_SSD"
  source_image_family = { image_family = var.source_image_family }
}
