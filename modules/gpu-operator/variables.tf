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

variable "device_plugin_enabled" {
  description = "Whether to explicitly enable the legacy NVIDIA device plugin. Set false when NVIDIA DRA owns GPU allocation."
  type        = bool
  default     = null
}

variable "driver_manager_env" {
  description = "Environment variables passed to the GPU Operator driver manager."
  type = list(object({
    name  = string
    value = string
  }))
  default = []
}

variable "toolkit_restart_mode" {
  description = "Optional method used by NVIDIA Container Toolkit to apply container runtime configuration changes."
  type        = string
  default     = null

  validation {
    condition     = var.toolkit_restart_mode == null || contains(["none", "signal", "systemd"], coalesce(var.toolkit_restart_mode, "none"))
    error_message = "toolkit_restart_mode must be none, signal, systemd, or null."
  }
}

variable "toolkit_config_source" {
  description = "Optional source used by NVIDIA Container Toolkit to read the container runtime configuration."
  type        = string
  default     = null

  validation {
    condition     = var.toolkit_config_source == null || contains(["command", "file"], coalesce(var.toolkit_config_source, "file"))
    error_message = "toolkit_config_source must be command, file, or null."
  }
}
