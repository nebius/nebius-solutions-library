variable "company_name" {
  description = "Name of the company."
  type        = string
}

variable "k8s_cluster_context" {
  description = "K8s context name for kubectl."
  type        = string
  nullable    = false
}

variable "o11y_iam_tenant_id" {
  description = "Tenant id for o11y."
  type        = string
  nullable    = false
}

variable "iam_project_id" {
  description = "ID of the IAM project of slurm cluster (not o11y)."
  type        = string
  nullable    = false
}

variable "o11y_secret_name" {
  description = "Secret name inside k8s cluster for o11y static key."
  type        = string
  default     = "o11y-writer-sa-token"
}

variable "o11y_secret_logs_namespace" {
  description = "Secret namespace inside k8s cluster for o11y static key."
  type        = string
  default     = "logs-system"
}
variable "o11y_secret_monitoring_namespace" {
  description = "Secret namespace inside k8s cluster for o11y static key."
  type        = string
  default     = "monitoring-system"
}

variable "o11y_profile" {
  description = "Profile for nebius CLI for o11y."
  type        = string
  nullable    = false
}

variable "opentelemetry_collector_cm" {
  description = "Configmap name for opentelemetry collector values"
  type        = string
  default     = "terraform-opentelemetry-collector"
}
