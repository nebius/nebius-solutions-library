variable "bootstrap_managed_externally" {
  description = "Whether the foundation-only Flux bootstrap was installed before node creation."
  type        = bool
  default     = false
}

moved {
  from = helm_release.soperator_fluxcd_bootstrap
  to   = helm_release.soperator_fluxcd_bootstrap[0]
}

variable "external_bootstrap_ready" {
  description = "Completion token for Flux takeover after the full values ConfigMap is written."
  type        = string
  default     = null
}

resource "terraform_data" "external_bootstrap" {
  input = var.external_bootstrap_ready
}

output "fluxcd_values_ready" {
  value = helm_release.soperator_fluxcd_cm.id
}

resource "terraform_data" "wait_for_bootstrap_releases" {
  count = var.bootstrap_managed_externally ? 1 : 0

  # Check again on each apply, including after node group updates.
  triggers_replace = [timestamp()]

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = "bash \"${path.module}/scripts/wait_for_bootstrap_releases.sh\""
    environment = {
      K8S_CONTEXT = var.k8s_cluster_context
      NAMESPACE   = var.flux_namespace
    }
  }
}
