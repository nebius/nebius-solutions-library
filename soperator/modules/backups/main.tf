resource "nebius_iam_v1_service_account" "backups_service_account" {
  parent_id = var.iam_project_id
  name      = "${var.instance_name}-backup-sa"
}

resource "terraform_data" "backups_service_account_ready" {
  triggers_replace = {
    service_account_id = nebius_iam_v1_service_account.backups_service_account.id
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    environment = {
      SERVICE_ACCOUNT_ID = self.triggers_replace.service_account_id
    }
    command = <<-EOT
      "${path.module}/../scripts/retry.sh" -n 30 -i 5 -- \
        nebius iam service-account get --id "$SERVICE_ACCOUNT_ID" >/dev/null
    EOT
  }
}

# TODO: replace it with more granular access binding as it becomes available
data "nebius_iam_v1_group" "editors" {
  name      = "editors"
  parent_id = var.iam_tenant_id
}

resource "nebius_iam_v1_group_membership" "backups_service_account_group" {
  depends_on = [terraform_data.backups_service_account_ready]

  parent_id = data.nebius_iam_v1_group.editors.id
  member_id = nebius_iam_v1_service_account.backups_service_account.id
}

# TODO: replace this mess with proper nebius provider resources as they become available
resource "terraform_data" "k8s_backups_bucket_access_secret" {
  depends_on = [terraform_data.backups_service_account_ready]

  triggers_replace = {
    namespace           = var.soperator_namespace
    secret_name         = local.secret_name
    k8s_cluster_context = var.k8s_cluster_context
    k8s_cluster_id      = var.k8s_cluster_id
    service_account_id  = nebius_iam_v1_service_account.backups_service_account.id
  }

  provisioner "local-exec" {
    when        = destroy
    interpreter = ["/bin/bash"]
    environment = {
      K8S_CLUSTER_CONTEXT = self.triggers_replace.k8s_cluster_context
      K8S_CLUSTER_ID      = try(self.triggers_replace.k8s_cluster_id, "")
      NAMESPACE           = self.triggers_replace.namespace
      SECRET_NAME         = self.triggers_replace.secret_name
      SERVICE_ACCOUNT_ID  = self.triggers_replace.service_account_id
    }
    command = "${path.module}/scripts/destroy.sh"
  }

  provisioner "local-exec" {
    when        = create
    interpreter = ["/bin/bash"]
    environment = {
      K8S_CLUSTER_CONTEXT = var.k8s_cluster_context
      IAM_PROJECT_ID      = var.iam_project_id
      NAMESPACE           = var.soperator_namespace
      SECRET_NAME         = local.secret_name
      SERVICE_ACCOUNT_ID  = nebius_iam_v1_service_account.backups_service_account.id
      BACKUPS_PASSWORD    = var.backups_password
    }
    command = "${path.module}/scripts/create.sh"
  }
}

output "secret_name" {
  value = local.secret_name
}
