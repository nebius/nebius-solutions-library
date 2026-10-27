variable "iam_project_id" {
  description = "ID of the IAM project."
  type        = string
}

variable "backups_bucket_id" {
  description = "ID of the backup bucket in the IAM project. Backup permissions are limited to this bucket."
  type        = string
}

variable "instance_name" {
  description = "Cluster instance name to distinguish between multiple clusters in tenant."
  type        = string
}

variable "k8s_cluster_context" {
  description = "K8s context name for kubectl."
  type        = string
}

variable "k8s_cluster_id" {
  description = "ID of the K8s cluster, used to set up kubectl context on destroy."
  type        = string
}

variable "soperator_namespace" {
  description = "Kubernetes namespace to look for jail in."
  type        = string
}

variable "backups_password" {
  description = "Password to encrypt backups."
  type        = string
  sensitive   = true
}
