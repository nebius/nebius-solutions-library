variable "k8s_cluster_context" {
  description = "The context of the Kubernetes cluster."
  type        = string
}

variable "flux_version" {
  description = "The version of Flux to install."
  type        = string
  default     = "v2.9.3"

  validation {
    condition     = can(regex("^v[0-9]+\\.[0-9]+\\.[0-9]+$", var.flux_version))
    error_message = "flux_version must be a stable semantic version such as v2.9.3."
  }
}
