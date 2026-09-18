resource "nebius_compute_v1_filesystem" "shared-filesystem" {
  count            = var.enable_filestore && var.existing_filestore == "" ? 1 : 0
  parent_id        = var.parent_id
  name             = join("-", ["filesystem-tf", local.release-suffix])
  type             = var.filestore_disk_type
  size_bytes       = provider::units::from_gib(var.filestore_disk_size_gibibytes)
  block_size_bytes = provider::units::from_kib(var.filestore_block_size_kibibytes)
  forbid_deletion  = var.filestore_forbid_deletion

  lifecycle {
    ignore_changes = [
      labels,
    ]
  }
}

data "nebius_compute_v1_filesystem" "shared-filesystem" {
  count = var.enable_filestore && var.existing_filestore != "" ? 1 : 0
  id    = var.existing_filestore
}

locals {
  shared-filesystem = var.enable_filestore ? {
    id = try(
      one(nebius_compute_v1_filesystem.shared-filesystem).id,
      one(data.nebius_compute_v1_filesystem.shared-filesystem).id,
    )
    size_gibibytes = floor(provider::units::to_gib(try(
      one(nebius_compute_v1_filesystem.shared-filesystem).status.size_bytes,
      one(data.nebius_compute_v1_filesystem.shared-filesystem).status.size_bytes,
    )))
    mount_tag = local.filestore.mount_tag
  } : null
}

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
    nebius_mk8s_v1_node_group.cpu-only,
    nebius_mk8s_v1_node_group.gpu,
    nebius_mk8s_v1_node_group.gb300,
  ]
}

resource "kubernetes_annotations" "filesystem_csi_demote_previous_default_storage_class" {
  count = local.filesystem_csi_enabled && var.filesystem_csi.make_default_storage_class && local.filesystem_csi_previous_default_sc != null && local.filesystem_csi_previous_default_sc != "" && local.filesystem_csi_previous_default_sc != local.filesystem_csi_storage_class_name ? 1 : 0

  api_version = "storage.k8s.io/v1"
  kind        = "StorageClass"
  force       = true

  metadata {
    name = local.filesystem_csi_previous_default_sc
  }

  annotations = {
    "storageclass.kubernetes.io/is-default-class" = "false"
  }

  depends_on = [
    helm_release.filesystem_csi,
  ]
}

resource "kubernetes_annotations" "filesystem_csi_promote_default_storage_class" {
  count = local.filesystem_csi_enabled && var.filesystem_csi.make_default_storage_class ? 1 : 0

  api_version = "storage.k8s.io/v1"
  kind        = "StorageClass"
  force       = true

  metadata {
    name = local.filesystem_csi_storage_class_name
  }

  annotations = {
    "storageclass.kubernetes.io/is-default-class" = "true"
  }

  depends_on = [
    helm_release.filesystem_csi,
    kubernetes_annotations.filesystem_csi_demote_previous_default_storage_class,
  ]
}
