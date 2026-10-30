variable "config" {
  description = "One native Run:ai control-plane or worker release. Prepare its prerequisites before installation."
  type = object({
    role                        = string
    domain                      = string
    version                     = optional(string, "3.0.20")
    chart_path                  = optional(string)
    values_files                = optional(list(string), [])
    install_ingress             = optional(bool, true)
    ingress_class               = optional(string, "haproxy")
    ingress_chart_path          = optional(string)
    ingress_values_files        = optional(list(string), [])
    install_prometheus_operator = optional(bool, true)
    prometheus_values_files     = optional(list(string), [])
    prometheus_chart_path       = optional(string)
    control_plane_url           = optional(string)
    cluster_uid                 = optional(string)
    bootstrap_token_revision    = optional(number, 1)
    serving = optional(object({
      inference_domain      = string
      tls_secret_name       = optional(string, "runai-cluster-inference-tls")
      install_operator      = optional(bool, true)
      operator_version      = optional(string, "1.23.0")
      operator_chart_path   = optional(string)
      version               = optional(string, "1.23.0")
      operator_values_files = optional(list(string), [])
      values_files          = optional(list(string), [])
    }))
  })

  validation {
    condition     = contains(["control-plane", "worker"], var.config.role)
    error_message = "Run:ai role must be control-plane or worker."
  }

  validation {
    condition     = trimspace(var.config.domain) != "" && !strcontains(var.config.domain, "://")
    error_message = "domain must be a DNS name without a URL scheme."
  }

  validation {
    condition = var.config.role != "worker" || (
      try(startswith(var.config.control_plane_url, "https://"), false) &&
      try(trimspace(var.config.cluster_uid) != "", false) &&
      var.config.bootstrap_token_revision >= 1
    )
    error_message = "A worker needs an HTTPS control_plane_url, registered cluster_uid and positive bootstrap_token_revision."
  }

  validation {
    condition = var.config.serving == null ? true : (
      var.config.role == "worker" &&
      startswith(var.config.serving.inference_domain, "*.") &&
      !strcontains(var.config.serving.inference_domain, "://") &&
      trimspace(var.config.serving.tls_secret_name) != ""
    )
    error_message = "serving requires a worker role, wildcard inference_domain and existing TLS Secret name."
  }
}

variable "bootstrap_token" {
  description = "Fresh native worker bootstrap token. Increment config.bootstrap_token_revision for each worker install/upgrade. Helm release storage still contains this credential."
  type        = string
  default     = null
  sensitive   = true
  ephemeral   = true

  validation {
    condition     = var.config.role != "worker" || try(trimspace(var.bootstrap_token) != "", false)
    error_message = "A worker needs a fresh bootstrap_token from its registered control-plane cluster record."
  }
}
