resource "nebius_compute_v1_disk" "dsvm-boot-disk" {
  parent_id           = var.parent_id
  name                = "dsvm-boot-disk"
  block_size_bytes    = 4096
  size_bytes          = 100 * 1024 * 1024 * 1024 # 100GiB
  type                = "NETWORK_SSD"
  source_image_family = { image_family = local.boot_image_family }
}
