# SA for Loki module
data "nebius_iam_v1_group" "loki_editors" {
  count     = var.o11y.loki.enabled ? 1 : 0
  name      = "editors"
  parent_id = var.tenant_id
}

resource "nebius_iam_v1_service_account" "loki_s3_sa" {
  count     = var.o11y.loki.enabled ? 1 : 0
  parent_id = var.parent_id
  name      = "loki_s3_sa-${var.cluster_id}"
}

resource "nebius_iam_v1_group_membership" "loki_sa_storage_editor" {
  count     = var.o11y.loki.enabled ? 1 : 0
  parent_id = data.nebius_iam_v1_group.loki_editors[0].id
  member_id = nebius_iam_v1_service_account.loki_s3_sa[count.index].id
}

resource "nebius_iam_v2_access_key" "loki_s3_key" {
  count                = var.o11y.loki.enabled ? 1 : 0
  parent_id            = var.parent_id
  name                 = "loki-s3-access-key"
  description          = "Access key for Loki module"
  secret_delivery_mode = "MYSTERY_BOX"
  account = {
    service_account = {
      id = nebius_iam_v1_service_account.loki_s3_sa[count.index].id
    }
  }
}

resource "random_string" "loki_unique_id" {
  count   = var.o11y.loki.enabled ? 1 : 0
  length  = 2
  upper   = false
  lower   = true
  numeric = true
  special = false
}

resource "nebius_storage_v1_bucket" "loki-bucket-chunks" {
  count             = var.o11y.loki.enabled ? 1 : 0
  parent_id         = var.parent_id
  name              = "loki-${var.cluster_id}-${random_string.loki_unique_id[0].result}-chunks"
  versioning_policy = "DISABLED"
}

resource "nebius_storage_v1_bucket" "loki-bucket-ruler" {
  count             = var.o11y.loki.enabled ? 1 : 0
  parent_id         = var.parent_id
  name              = "loki-${var.cluster_id}-${random_string.loki_unique_id[0].result}-ruler"
  versioning_policy = "DISABLED"
}

resource "nebius_storage_v1_bucket" "loki-bucket-admin" {
  count             = var.o11y.loki.enabled ? 1 : 0
  parent_id         = var.parent_id
  name              = "loki-${var.cluster_id}-${random_string.loki_unique_id[0].result}-admin"
  versioning_policy = "DISABLED"
}

resource "nebius_applications_v1alpha1_k8s_release" "loki" {
  count = var.o11y.loki.enabled ? 1 : 0

  cluster_id = var.cluster_id
  parent_id  = var.parent_id

  application_name = "loki"
  namespace        = var.namespace
  product_slug     = "nebius/loki"

  sensitive = {
    set = {
      "loki.storage.bucketPrefix" : "loki-${var.cluster_id}-${random_string.loki_unique_id[0].result}",
      "loki.storage.s3.region" : var.o11y.loki.region,
      "loki.commonConfig.replication_factor" : var.o11y.loki.replication_factor,
      "loki.storage.s3.accessKeyId" : nebius_iam_v2_access_key.loki_s3_key[0].status.aws_access_key_id
      "loki.storage.s3.secretAccessKey" : ephemeral.nebius_mysterybox_v1_secret_payload_entry.loki_s3_secret[0].data.string_value
    }
  }
}