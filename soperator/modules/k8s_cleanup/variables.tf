variable "k8s_cluster_context" {
  description = "K8s context name for kubectl."
  type        = string
}

variable "k8s_cluster_id" {
  description = "ID of the K8s cluster, used to set up kubectl context on destroy."
  type        = string
}

variable "soperator_namespace" {
  description = "Namespace where soperator resources live."
  type        = string
  default     = "soperator"
}

variable "login_service_name" {
  description = "Name of the login service to delete before VPC resources are destroyed."
  type        = string
  default     = "soperator-login-svc"
}

variable "webhook_prefix" {
  description = "Prefix for mutating/validating webhook configurations to delete on destroy."
  type        = string
  default     = "kruise"
}
