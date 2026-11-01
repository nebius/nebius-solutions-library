variable "parent_id" {
  description = "Project ID."
  type        = string
}

variable "cluster_id" {
  description = "Existing regional Kubernetes cluster ID."
  type        = string
}

variable "config" {
  description = "Registered Run:ai worker installed through the canonical Marketplace application."
  type = object({
    role                        = optional(string, "worker")
    domain                      = string
    values                      = optional(string)
    install_ingress             = optional(bool, true)
    ingress_class               = optional(string, "nginx")
    ingress_values              = optional(string)
    install_prometheus_operator = optional(bool, true)
    prometheus_values           = optional(string)
    control_plane_url           = string
    cluster_uid                 = string
    bootstrap_token_revision    = optional(number, 1)
    serving = optional(object({
      inference_domain = string
      tls_secret_name  = optional(string, "runai-cluster-inference-tls")
      install_operator = optional(bool, true)
      operator_version = optional(string, "1.23.0")
      operator_values  = optional(string)
      spec             = string
    }))
  })

  validation {
    condition     = var.config.role == "worker"
    error_message = "This module installs workers. Install the full control-plane application through Marketplace Console/API."
  }

  validation {
    condition     = trimspace(var.config.domain) != "" && !strcontains(var.config.domain, "://")
    error_message = "domain must be a DNS name without a URL scheme."
  }

  validation {
    condition = (
      try(startswith(var.config.control_plane_url, "https://"), false) &&
      try(trimspace(var.config.cluster_uid) != "", false) &&
      var.config.bootstrap_token_revision >= 1
    )
    error_message = "A worker needs an HTTPS control_plane_url, registered cluster_uid and positive bootstrap_token_revision."
  }

  validation {
    condition = var.config.serving == null ? true : (
      startswith(var.config.serving.inference_domain, "*.") &&
      !strcontains(var.config.serving.inference_domain, "://") &&
      trimspace(var.config.serving.tls_secret_name) != ""
    )
    error_message = "serving requires a worker role, wildcard inference_domain and existing TLS Secret name."
  }
}

variable "bootstrap_token" {
  description = "Fresh native bootstrap token, written only to a caller-managed Kubernetes Secret. Increment config.bootstrap_token_revision for rotation."
  type        = string
  default     = null
  sensitive   = true
  ephemeral   = true

  validation {
    condition     = try(trimspace(var.bootstrap_token) != "", false)
    error_message = "A worker needs a fresh bootstrap_token from its registered control-plane cluster record."
  }
}
