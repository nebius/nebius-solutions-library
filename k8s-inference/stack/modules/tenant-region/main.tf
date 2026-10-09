# One tenant in one region project: service account + group (storage.object-editor on the tenant bucket
# through a bucket policy), the bucket with an artifact-retention lifecycle rule, and the S3 access key the
# tenant namespace uses (Secrets tenant-storage / s3). `protect_data` makes the bucket refuse destroy.
terraform {
  required_providers {
    nebius = { source = "nebius/nebius" }
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

# Disposable bucket (protect_data = false): emptied before Terraform deletes it (Nebius refuses to delete a
# non-empty bucket). This resource depends on the bucket and the key, so `destroy` runs it first, while the
# key still exists. Needs the AWS CLI on the machine that runs the destroy.
resource "terraform_data" "empty_bucket" {
  count = var.protect_data ? 0 : 1
  input = {
    script   = "${path.module}/../../scripts/empty-bucket.sh"
    endpoint = local.endpoint
    bucket   = nebius_storage_v1_bucket.disposable[0].name
    key      = nebius_iam_v2_access_key.tenant.status.aws_access_key_id
    secret   = nebius_iam_v2_access_key.tenant.status.secret
  }
  provisioner "local-exec" {
    when    = destroy
    command = "${self.input.script} ${self.input.endpoint} ${self.input.bucket}"
    environment = {
      AWS_ACCESS_KEY_ID     = self.input.key
      AWS_SECRET_ACCESS_KEY = self.input.secret
    }
  }
}

resource "nebius_iam_v2_access_key" "tenant" {
  parent_id            = var.project_id
  name                 = "${var.name}-key"
  description          = "S3 credentials of tenant ${var.tenant} in ${var.region} (Secrets tenant-storage and s3)"
  secret_delivery_mode = "INLINE"
  account              = { service_account = { id = nebius_iam_v1_service_account.tenant.id } }
}

# The bucket's S3 host and region come from its status (the region name is the fallback until the first apply).
locals {
  bucket   = var.protect_data ? nebius_storage_v1_bucket.protected[0] : nebius_storage_v1_bucket.disposable[0]
  host     = coalesce(try(local.bucket.status.domain_name, null), "storage.${var.region}.nebius.cloud")
  endpoint = "https://${local.host}"
}

output "storage" {
  sensitive = true
  value = {
    bucket     = var.name
    endpoint   = local.endpoint
    region     = coalesce(try(local.bucket.status.region, null), var.region)
    access_key = nebius_iam_v2_access_key.tenant.status.aws_access_key_id
    secret_key = nebius_iam_v2_access_key.tenant.status.secret
  }
}
output "service_account_id" { value = nebius_iam_v1_service_account.tenant.id }
output "bucket" { value = var.name }
output "bucket_host" { value = local.host }
