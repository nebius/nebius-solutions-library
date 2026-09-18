variable "parent_id" {
  description = "Project ID."
  type        = string
}

variable "ngc_key" {
  description = "API key from Nvidia GPU cloud: catalog.ngc.nvidia.com"
  type        = string
  default     = ""
}

variable "namespace" {
  description = "NIM namespace."
  type        = string
  default     = "nims"
}

variable "model_catalog" {
  description = <<-EOT
    Per-model catalog overrides. Keys can override entries from the built-in
    catalog or add new NIM models without adding Terraform resources, variables,
    or hand-assigned proxy ports.
  EOT
  type        = any
  default     = {}
}

variable "service_monitor_labels" {
  description = "Additional labels applied to generated ServiceMonitor resources."
  type        = map(string)
  default     = {}
}

variable "service_monitor_interval" {
  description = "Scrape interval for generated NIM ServiceMonitor endpoints."
  type        = string
  default     = "15s"
}

variable "service_monitor_scrape_timeout" {
  description = "Scrape timeout for generated NIM ServiceMonitor endpoints."
  type        = string
  default     = "10s"
}
