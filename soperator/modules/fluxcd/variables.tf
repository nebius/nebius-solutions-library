variable "k8s_cluster_context" {
  description = "The context of the Kubernetes cluster."
  type        = string
}

variable "flux_version" {
  description = "The version of Flux to install."
  type        = string
  default     = "v2.7.4"
}
