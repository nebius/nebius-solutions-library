# One tenant in one region project: service account + group (storage.object-editor on the tenant bucket
# through a bucket policy), the bucket with an artifact-retention lifecycle rule, and the S3 access key the
# tenant namespace uses (Secrets tenant-storage / s3). `protect_data` makes the bucket refuse destroy.
terraform {
  required_providers {
    nebius = { source = "terraform-provider.storage.eu-north1.nebius.cloud/nebius/nebius" }
  }
}

resource "nebius_iam_v1_service_account" "tenant" {
  parent_id   = var.project_id
  name        = var.name
  description = "Serverless 2.0 tenant ${var.tenant}: bucket access in ${var.region}"
  labels      = var.labels
}

resource "nebius_iam_v1_group" "tenant" {
  parent_id = var.project_id
  name      = var.name
}

resource "nebius_iam_v1_group_membership" "tenant" {
  parent_id = nebius_iam_v1_group.tenant.id
  member_id = nebius_iam_v1_service_account.tenant.id
}

locals {
  policy = { rules = [{ group_id = nebius_iam_v1_group.tenant.id, paths = ["*"], roles = ["storage.object-editor"] }] }
  lifecycle = var.lifecycle_days > 0 ? {
    rules = [{
      id         = "expire-run-outputs", status = "ENABLED", filter = { prefix = "operations/" }
      expiration = { days = var.lifecycle_days }, abort_incomplete_multipart_upload = { days_after_initiation = 7 }
    }]
  } : null
}

resource "nebius_storage_v1_bucket" "protected" {
  count                   = var.protect_data ? 1 : 0
  parent_id               = var.project_id
  name                    = var.name
  labels                  = var.labels
  versioning_policy       = "DISABLED"
  bucket_policy           = local.policy
  lifecycle_configuration = local.lifecycle
  lifecycle { prevent_destroy = true }
}

resource "nebius_storage_v1_bucket" "disposable" {
  count                   = var.protect_data ? 0 : 1
  parent_id               = var.project_id
  name                    = var.name
  labels                  = var.labels
  versioning_policy       = "DISABLED"
  bucket_policy           = local.policy
  lifecycle_configuration = local.lifecycle
}

resource "nebius_iam_v2_access_key" "tenant" {
  parent_id            = var.project_id
  name                 = "${var.name}-key"
  description          = "S3 credentials of tenant ${var.tenant} in ${var.region} (Secrets tenant-storage and s3)"
  secret_delivery_mode = "INLINE"
  account              = { service_account = { id = nebius_iam_v1_service_account.tenant.id } }
}

output "storage" {
  sensitive = true
  value = {
    bucket     = var.name
    endpoint   = "https://storage.${var.region}.nebius.cloud"
    region     = var.region
    access_key = nebius_iam_v2_access_key.tenant.status.aws_access_key_id
    secret_key = nebius_iam_v2_access_key.tenant.status.secret
  }
}
output "service_account_id" { value = nebius_iam_v1_service_account.tenant.id }
output "bucket" { value = var.name }
