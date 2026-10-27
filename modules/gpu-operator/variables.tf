variable "cluster_id" {
  description = "K8s cluster id."
  type        = string
}

variable "parent_id" {
  description = "Project id."
  type        = string
}

variable "enable_dcgm_exporter" {
  description = "Whether to enable DCGM exporter."
  type        = bool
  default     = false
}

variable "enable_dcgm_service_monitor" {
  description = "Whether to enable DCGM service monitor."
  type        = bool
  default     = false
}

variable "relabel_dcgm_exporter" {
  description = "Whether to add 'app.kubernetes.io/name' label to DCGM metrics"
  type        = bool
  default     = false
}

variable "mig_strategy" {
  description = "MIG strategy for GPU nodes."
  type        = string
  default     = null

  validation {
    condition     = var.mig_strategy == null || contains(["none", "single", "mixed"], coalesce(var.mig_strategy, "null"))
    error_message = "Invalid MIG strategy '${coalesce(var.mig_strategy, "null")}'. Must be one of ['none', 'single', 'mixed'] or left unset."
  }
}

variable "cdi_enabled" {
  description = "Whether to explicitly enable CDI for the GPU Operator."
  type        = bool
  default     = null
}
