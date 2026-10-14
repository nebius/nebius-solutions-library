variable "helm_repository" {
  description = "GPU Operator Helm chart source repository."
  type        = string
  default     = "https://helm.ngc.nvidia.com/nvidia"
}

variable "helm_version" {
  description = "Version of GPU Operator Helm chart."
  type        = string
  default     = "v25.10.0"
}

variable "driver_version" {
  description = "NVIDIA driver version."
  type        = string
  default     = "580.95.05"
}

variable "nfd_enabled" {
  description = "Enable Node Feature Discovery."
  type        = bool
  default     = true
}

variable "rdma_enabled" {
  description = "Enable RDMA support for GPUDirect/InfiniBand (loads nvidia_peermem module)."
  type        = bool
  default     = true
}

variable "rdma_use_host_mofed" {
  description = "Use host's Mellanox OFED driver for InfiniBand instead of containerized version."
  type        = bool
  default     = true
}

variable "dcgm_exporter_enabled" {
  description = "Enable DCGM Exporter for Prometheus GPU metrics."
  type        = bool
  default     = true
}

variable "dcgm_service_monitor_enabled" {
  description = "Enable ServiceMonitor for DCGM Exporter (requires Prometheus Operator)."
  type        = bool
  default     = false
}

variable "mig_strategy" {
  description = "MIG (Multi-Instance GPU) strategy: 'none', 'single', or 'mixed'."
  type        = string
  default     = "none"

  validation {
    condition     = contains(["none", "single", "mixed"], var.mig_strategy)
    error_message = "MIG strategy must be one of: 'none', 'single', 'mixed'."
  }
}

variable "cdi_enabled" {
  description = "Whether to explicitly enable CDI for the GPU Operator."
  type        = bool
  default     = null
}

variable "gds_enabled" {
  description = "Enable GPU Direct Storage support."
  type        = bool
  default     = false
}
