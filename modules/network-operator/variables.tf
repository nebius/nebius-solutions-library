variable "cluster_id" {
  description = "K8s cluster id."
  type        = string
}

variable "parent_id" {
  description = "Project id."
  type        = string
}

variable "limit_cpu" {
  description = "CPU limit for the Network Operator"
  type        = string
  default     = "500m"
}

variable "limit_memory" {
  description = "Memory limit for the Network Operator"
  type        = string
  default     = "1280Mi"
}

variable "values" {
  description = "Optional native Helm values YAML content for the Marketplace application."
  type        = string
  default     = null
}
