resource "nebius_iam_v1_service_account" "checkpoints_service_account" {
  parent_id = var.iam_project_id
  name      = "${var.instance_name}-checkpoints-sa"
}

# Keep checkpoint credentials least-privileged. Use a project-scoped group for
# the common same-project case; the caller selects tenant scope only when a
# replacement cluster reuses a bucket from another project in the same tenant.
resource "nebius_iam_v1_group" "checkpoints_group" {
  name      = "${var.instance_name}-checkpoints"
  parent_id = var.iam_group_parent_id
}

resource "nebius_iam_v1_group_membership" "checkpoints_service_account_group" {
  parent_id = nebius_iam_v1_group.checkpoints_group.id
  member_id = nebius_iam_v1_service_account.checkpoints_service_account.id
}

resource "nebius_iam_v1_access_permit" "checkpoints_bucket_object_editor" {
  parent_id   = nebius_iam_v1_group.checkpoints_group.id
  resource_id = var.bucket_id
  role        = "storage.object-editor"
}

resource "nebius_iam_v2_access_key" "checkpoints_access_key" {
  parent_id = var.iam_project_id
  name      = "${var.instance_name}-checkpoints-key"
  account = {
    service_account = {
      id = nebius_iam_v1_service_account.checkpoints_service_account.id
    }
  }
  # The secret is delivered via a MysteryBox reference instead of inline, so it
  # never enters the Terraform state. create.sh fetches the payload ephemerally
  # and puts it straight into the k8s secret.
  secret_delivery_mode = "MYSTERY_BOX"

  depends_on = [
    nebius_iam_v1_group_membership.checkpoints_service_account_group,
    nebius_iam_v1_access_permit.checkpoints_bucket_object_editor,
  ]
}

# The secret is managed with kubectl scripts rather than kubernetes provider resources
# so that destroy stays graceful when the k8s cluster is already gone.
resource "terraform_data" "k8s_checkpoints_secret" {
  count = var.create_k8s_secret ? 1 : 0

  triggers_replace = {
    namespace           = var.soperator_namespace
    secret_name         = local.secret_name
    k8s_cluster_context = var.k8s_cluster_context
    k8s_cluster_id      = var.k8s_cluster_id
    service_account_id  = nebius_iam_v1_service_account.checkpoints_service_account.id
    access_key_id       = nebius_iam_v2_access_key.checkpoints_access_key.id
    bucket_name         = var.bucket_name
    bucket_endpoint     = var.bucket_endpoint
    region              = var.region
    jail_env_file_owner = var.jail_env_file_owner
    jail_env_file_mode  = var.jail_env_file_mode
    # Re-run the provisioners when the scripts change, so existing
    # installations pick up fixes (e.g. the credentials renderer).
    scripts_sha = sha256(join("", [
      filesha256("${path.module}/scripts/create.sh"),
      filesha256("${path.module}/scripts/destroy.sh"),
    ]))
  }

  provisioner "local-exec" {
    when        = destroy
    interpreter = ["/bin/bash"]
    environment = {
      K8S_CLUSTER_CONTEXT = self.triggers_replace.k8s_cluster_context
      K8S_CLUSTER_ID      = try(self.triggers_replace.k8s_cluster_id, "")
      NAMESPACE           = self.triggers_replace.namespace
      SECRET_NAME         = self.triggers_replace.secret_name
    }
    command = "${path.module}/scripts/destroy.sh"
  }

  provisioner "local-exec" {
    when        = create
    interpreter = ["/bin/bash"]
    environment = {
      K8S_CLUSTER_CONTEXT     = var.k8s_cluster_context
      NAMESPACE               = var.soperator_namespace
      SECRET_NAME             = local.secret_name
      SERVICE_ACCOUNT_ID      = nebius_iam_v1_service_account.checkpoints_service_account.id
      ACCESS_KEY_ID_VAL       = nebius_iam_v2_access_key.checkpoints_access_key.status.aws_access_key_id
      SECRET_REFERENCE_ID     = nebius_iam_v2_access_key.checkpoints_access_key.status.secret_reference_id
      OBJECT_STORAGE_ENDPOINT = var.bucket_endpoint
      CHECKPOINT_BUCKET       = var.bucket_name
      OBJECT_STORAGE_REGION   = var.region
      JAIL_ENV_FILE_OWNER     = var.jail_env_file_owner
      JAIL_ENV_FILE_MODE      = var.jail_env_file_mode
    }
    command = "${path.module}/scripts/create.sh"
  }
}

output "secret_name" {
  value = local.secret_name
}

output "service_account_id" {
  value = nebius_iam_v1_service_account.checkpoints_service_account.id
}

output "access_key_id" {
  description = "Object Storage access key ID of the checkpoints service account."
  value       = nebius_iam_v2_access_key.checkpoints_access_key.status.aws_access_key_id
}

output "secret_reference_id" {
  description = "MysteryBox secret ID holding the access key secret. With create_k8s_secret = false, fetch the payload yourself: nebius mysterybox v1 payload get --secret-id <id>."
  value       = nebius_iam_v2_access_key.checkpoints_access_key.status.secret_reference_id
}
