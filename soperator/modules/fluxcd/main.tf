resource "terraform_data" "flux_namespace" {
  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    # Render the namespace declaratively so an apply can be retried after an
    # ambiguous API response without failing with AlreadyExists.
    command = <<-EOT
      set -eo pipefail

      kubectl create namespace flux-system \
        --context "${var.k8s_cluster_context}" \
        --dry-run=client \
        -o yaml \
        | "${path.module}/../scripts/kubectl_apply_with_retries.sh" \
          --context "${var.k8s_cluster_context}"
    EOT
  }
  triggers_replace = {
    first_run = "true"
  }
}

resource "terraform_data" "flux2" {
  depends_on = [terraform_data.flux_namespace]
  provisioner "local-exec" {
    command = "${path.module}/../scripts/retry.sh -- ${path.module}/scripts/install.sh"
    environment = {
      FLUX_K8S_CONTEXT = var.k8s_cluster_context
      FLUX_VERSION     = var.flux_version
    }
  }
  triggers_replace = {
    flux_version = var.flux_version
    installer_sha256 = sha256(join("", [
      filesha256("${path.module}/scripts/install.sh"),
      filesha256("${path.module}/templates/kustomization.yaml"),
      filesha256("${path.module}/templates/migration-job.yaml"),
    ]))
  }
}
