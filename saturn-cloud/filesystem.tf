############################
# Shared Filesystem (Filestore)
############################

resource "nebius_compute_v1_filesystem" "shared_filesystem" {
  count            = var.enable_filestore && var.existing_filestore == "" ? 1 : 0
  parent_id        = var.project_id
  name             = "${var.cluster_name}-shared-fs"
  type             = var.filestore_disk_type
  size_bytes       = provider::units::from_gib(var.filestore_disk_size_gibibytes)
  block_size_bytes = provider::units::from_kib(var.filestore_block_size_kibibytes)

  lifecycle {
    ignore_changes = [labels]
  }
}

data "nebius_compute_v1_filesystem" "shared_filesystem" {
  count = var.enable_filestore && var.existing_filestore != "" ? 1 : 0
  id    = var.existing_filestore
}

############################
# CSI Driver (creates StorageClass: csi-mounted-fs-path-sc)
############################

resource "helm_release" "filesystem_csi" {
  count = local.filesystem_csi_enabled ? 1 : 0

  name             = local.filesystem_csi_chart_name
  repository       = "oci://cr.eu-north1.nebius.cloud/mk8s/helm"
  chart            = local.filesystem_csi_chart_name
  version          = var.filesystem_csi.chart_version
  namespace        = var.filesystem_csi.namespace
  create_namespace = true
  atomic           = true
  wait             = true

  set = [
    {
      name  = "dataDir"
      value = local.filesystem_csi_data_dir
    },
  ]

  depends_on = [
    nebius_mk8s_v1_node_group.system_nodes,
    nebius_mk8s_v1_node_group.pool,
  ]
}
