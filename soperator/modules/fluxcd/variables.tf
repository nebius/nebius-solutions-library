variable "k8s_cluster_context" {
  description = "The context of the Kubernetes cluster."
  type        = string
}

variable "flux_version" {
  description = "The version of Flux to install."
  type        = string
  default     = "v2.9.6"

  validation {
    condition     = var.flux_version == "v2.9.6"
    error_message = "The eMudhra-patched controller images are pinned for Flux v2.9.6; update the image overrides before using another Flux version."
  }
}
