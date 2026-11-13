variable "prometheus_url" {
  description = "URL of the existing Prometheus queried by OpenCost; no monitoring stack is installed."
  type        = string

  validation {
    condition     = can(regex("^https?://[^/]+", var.prometheus_url))
    error_message = "prometheus_url must be an HTTP or HTTPS URL."
  }
}

variable "namespace" {
  description = "Namespace for OpenCost."
  type        = string
  default     = "opencost"
}

variable "chart_version" {
  description = "Pinned upstream OpenCost chart version."
  type        = string
  default     = "2.5.33"
}

variable "chart_path" {
  description = "Optional local, pinned upstream chart archive."
  type        = string
  default     = null
}

variable "values" {
  description = "Native Helm values documents applied after the existing Prometheus connection settings."
  type        = list(string)
  default     = []
}
