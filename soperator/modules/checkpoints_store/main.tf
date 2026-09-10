locals {
  create      = var.bucket.existing == null
  bucket_name = var.bucket.existing == null ? coalesce(var.bucket.spec.name, "${var.instance_name}-checkpoints") : var.bucket.existing.name
  endpoint = (local.create
    ? "https://${nebius_storage_v1_bucket.checkpoints_bucket[0].status.domain_name}:443"
    : (var.bucket.existing != null
      ? coalesce(var.bucket.existing.endpoint, "https://storage.${var.region}.nebius.cloud:443")
      : "https://storage.${var.region}.nebius.cloud:443"
    )
  )
}

resource "nebius_storage_v1_bucket" "checkpoints_bucket" {
  count = local.create ? 1 : 0

  parent_id = var.iam_project_id
  name      = local.bucket_name

  # Interrupted large checkpoint transfers can leave billable multipart parts
  # that are invisible to ordinary object listing and retention. Clean those up
  # automatically while leaving completed checkpoint retention to the workload.
  lifecycle_configuration = {
    rules = [{
      id     = "abort-incomplete-checkpoint-uploads"
      status = "ENABLED"
      abort_incomplete_multipart_upload = {
        days_after_initiation = 7
      }
    }]
  }
}

data "nebius_storage_v1_bucket" "existing_checkpoint_bucket" {
  count = local.create ? 0 : 1

  parent_id = coalesce(var.bucket.existing.project_id, var.iam_project_id)
  name      = local.bucket_name
}

# Destroy-time cleanup: deleting a non-empty created bucket requires
# CHECKPOINTS_FORCE_CLEANUP=<bucket-name> in the destroy process environment.
# An environment variable is read at provisioner execution time, so it can
# never be stale like state- or plan-captured values, and it lives outside
# Terraform state and saved plans by construction. Without it, a non-empty
# bucket deliberately stops the destroy with retain/empty/force instructions.
resource "terraform_data" "cleanup_bucket" {
  count = local.create ? 1 : 0

  # NOTE: keep this trigger set exactly {bucket_name, endpoint}: it matches
  # already-deployed instances, so upgrading does not replace the resource
  # (which would run the destroy provisioner mid-upgrade).
  triggers_replace = {
    bucket_name = nebius_storage_v1_bucket.checkpoints_bucket[0].name
    endpoint    = local.endpoint
  }

  depends_on = [
    nebius_storage_v1_bucket.checkpoints_bucket
  ]

  provisioner "local-exec" {
    when        = destroy
    interpreter = ["/bin/bash", "-c"]
    command     = "bash '${path.module}/scripts/bucket_teardown.sh' cleanup '${self.triggers_replace.bucket_name}' '${self.triggers_replace.endpoint}'"
  }
}

output "name" {
  value = local.bucket_name
}

output "id" {
  description = "ID of the created or reused Nebius Object Storage bucket."
  value = (local.create
    ? nebius_storage_v1_bucket.checkpoints_bucket[0].id
    : data.nebius_storage_v1_bucket.existing_checkpoint_bucket[0].id
  )
}

output "endpoint" {
  value = local.endpoint
}
