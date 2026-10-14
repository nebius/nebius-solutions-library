resource "nebius_compute_v1_filesystem" "shared-filesystem" {
  for_each = var.enable_filestore ? {
    for cluster_key, cluster in local.cluster_config : cluster_key => cluster
    if cluster.existing_filestore == ""
  } : {}

  parent_id        = each.value.parent_id
  name             = join("-", ["filesystem-tf", each.value.region, local.release-suffix])
  type             = var.filestore_disk_type
  size_bytes       = provider::units::from_gib(var.filestore_disk_size_gibibytes)
  block_size_bytes = provider::units::from_kib(var.filestore_block_size_kibibytes)

  lifecycle {
    ignore_changes = [
      labels,
    ]
  }
}

data "nebius_compute_v1_filesystem" "shared-filesystem" {
  for_each = var.enable_filestore ? {
    for cluster_key, cluster in local.cluster_config : cluster_key => cluster
    if cluster.existing_filestore != ""
  } : {}

  id = each.value.existing_filestore
}

locals {
  shared-filesystem = var.enable_filestore ? {
    for cluster_key, cluster in local.cluster_config : cluster_key => {
      id = try(
        nebius_compute_v1_filesystem.shared-filesystem[cluster_key].id,
        data.nebius_compute_v1_filesystem.shared-filesystem[cluster_key].id,
      )
      size_gibibytes = floor(provider::units::to_gib(try(
        nebius_compute_v1_filesystem.shared-filesystem[cluster_key].status.size_bytes,
        data.nebius_compute_v1_filesystem.shared-filesystem[cluster_key].status.size_bytes,
      )))
      mount_tag = local.filestore.mount_tag
    }
  } : {}
}
