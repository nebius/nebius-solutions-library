variable "k8s_cluster_context" {
  type = string
}

variable "k8s_cluster_id" {
  type = string
}

variable "namespace" {
  type    = string
  default = "flux-system"
}

variable "slurm_namespace" {
  type = string
}

variable "operator_version" {
  type = string
}

variable "operator_stable" {
  type = bool
}

variable "full_values_ready" {
  description = "Completion token for the full Terraform values ConfigMap; gates Flux takeover."
  type        = string
}
