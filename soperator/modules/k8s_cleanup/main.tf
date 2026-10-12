resource "terraform_data" "login_service_cleanup" {
  triggers_replace = {
    k8s_cluster_context = var.k8s_cluster_context
    k8s_cluster_id      = var.k8s_cluster_id
    namespace           = var.soperator_namespace
    service_name        = var.login_service_name
  }

  provisioner "local-exec" {
    when        = destroy
    interpreter = ["/bin/bash", "-c"]

    environment = {
      "K8S_CLUSTER_CONTEXT" : self.triggers_replace.k8s_cluster_context,
      "K8S_CLUSTER_ID" : try(self.triggers_replace.k8s_cluster_id, ""),
      "SOPERATOR_NAMESPACE" : self.triggers_replace.namespace,
      "LOGIN_SERVICE_NAME" : self.triggers_replace.service_name,
    }
    command = "/bin/bash ${path.module}/scripts/k8s_soperator_cleanup.sh"
  }
}

resource "terraform_data" "kruise_webhook_cleanup" {
  triggers_replace = {
    k8s_cluster_context = var.k8s_cluster_context
    k8s_cluster_id      = var.k8s_cluster_id
    webhook_prefix      = var.webhook_prefix
  }

  provisioner "local-exec" {
    when        = destroy
    interpreter = ["/bin/bash", "-c"]

    environment = {
      "K8S_CLUSTER_CONTEXT" : self.triggers_replace.k8s_cluster_context,
      "K8S_CLUSTER_ID" : try(self.triggers_replace.k8s_cluster_id, ""),
      "K8S_WEBHOOK_PREFIX" : self.triggers_replace.webhook_prefix,
    }
    command = "/bin/bash ${path.module}/scripts/k8s_kruise_webhook_cleanup.sh"
  }
}
