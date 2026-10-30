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
    interpreter = ["/bin/bash", "-c"]
    command = join(
      " ",
      [
        "${path.module}/../scripts/retry.sh", "--",
        "kubectl", "--context", var.k8s_cluster_context,
        "apply", "-f", "https://github.com/fluxcd/flux2/releases/download/${var.flux_version}/install.yaml",
      ]
    )
  }
  triggers_replace = {
    first_run = "true"
  }
}
