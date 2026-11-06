variable "parent_id" {
  description = "Project ID."
  type        = string
}

variable "cluster_id" {
  description = "Existing regional Kubernetes cluster ID."
  type        = string
}

variable "region" {
  description = "Region of the Kubernetes cluster and its Nebius tunnel."
  type        = string
}

variable "network_id" {
  description = "Private network for control-plane PostgreSQL; unused by workers."
  type        = string
  default     = null
}

variable "config" {
  description = "Marketplace control plane or official NVIDIA regional worker. Register workers through Run:ai's native UI/API."
  type = object({
    role                        = optional(string, "worker")
    domain                      = optional(string)
    public_endpoint             = optional(bool)
    tunnel_public_key_file      = optional(string)
    tunnel_target               = optional(string, "runai-ingress-controller.ingress-nginx.svc.cluster.local:80")
    credentials_revision        = optional(number, 1)
    admin_email                 = optional(string, "admin@run.ai")
    values                      = optional(string)
    install_ingress             = optional(bool, true)
    ingress_class               = optional(string, "nginx")
    ingress_values              = optional(string)
    install_prometheus_operator = optional(bool, true)
    prometheus_values           = optional(string)
    control_plane_url           = optional(string)
    cluster_uid                 = optional(string, "")
    bootstrap_token_revision    = optional(number, 1)
    postgresql = optional(object({
      platform              = optional(string, "cpu-d3")
      preset                = optional(string, "4vcpu-16gb")
      host_count            = optional(number, 1)
      disk_size_gib         = optional(number, 20)
      backup_retention_days = optional(number, 7)
    }), {})
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
    condition     = contains(["control-plane", "worker"], var.config.role)
    error_message = "role must be control-plane or worker."
  }

  validation {
    condition = coalesce(var.config.public_endpoint, var.config.role == "control-plane") ? true : (
      var.config.domain == null ? false : trimspace(var.config.domain) != "" && !strcontains(var.config.domain, "://")
    )
    error_message = "Provide domain without a URL scheme, or enable public_endpoint."
  }

  validation {
    condition = var.config.role != "worker" || var.config.cluster_uid == "" ? true : (
      var.config.control_plane_url == null ? false : startswith(var.config.control_plane_url, "https://") &&
      var.config.bootstrap_token_revision >= 1
    )
    error_message = "A registered worker needs an HTTPS control_plane_url and positive bootstrap_token_revision."
  }

  validation {
    condition = coalesce(var.config.public_endpoint, var.config.role == "control-plane") ? (
      var.config.tunnel_public_key_file == null ? false : fileexists(var.config.tunnel_public_key_file)
    ) : true
    error_message = "A public endpoint needs tunnel_public_key_file containing the authorized 4096-bit RSA public key."
  }

  validation {
    condition     = var.config.credentials_revision >= 1
    error_message = "credentials_revision must be positive; increment it when rotating protected input files."
  }

  validation {
    condition = var.config.serving == null ? true : var.config.role == "worker" && (
      startswith(var.config.serving.inference_domain, "*.") &&
      !strcontains(var.config.serving.inference_domain, "://") &&
      trimspace(var.config.serving.tls_secret_name) != ""
    )
    error_message = "serving requires a worker role, wildcard inference_domain and existing TLS Secret name."
  }
}

variable "bootstrap_token" {
  description = "Fresh native bootstrap token passed through Helm write-only set_wo; Helm history retains it. Increment config.bootstrap_token_revision when refreshing it."
  type        = string
  default     = ""
  nullable    = false
  sensitive   = true
  ephemeral   = true

  validation {
    condition     = var.config.role != "worker" || var.config.cluster_uid == "" ? true : trimspace(var.bootstrap_token) != ""
    error_message = "A registered worker needs a fresh bootstrap_token from its control-plane cluster record."
  }
}

variable "admin_password" {
  description = "Control-plane admin password from a protected caller file; passed through Marketplace write-only values."
  type        = string
  default     = ""
  nullable    = false
  sensitive   = true
  ephemeral   = true

  validation {
    condition     = var.config.role != "control-plane" ? true : trimspace(var.admin_password) != ""
    error_message = "A control plane needs admin_password from a protected caller file."
  }
}

variable "database_password" {
  description = "Initial PostgreSQL password from a protected caller file; passed through provider write-only fields."
  type        = string
  default     = ""
  nullable    = false
  sensitive   = true
  ephemeral   = true

  validation {
    condition     = var.config.role != "control-plane" ? true : trimspace(var.database_password) != ""
    error_message = "A control plane needs database_password from a protected caller file."
  }
}

variable "tunnel_private_key_pem" {
  description = "4096-bit RSA private key matching config.tunnel_public_key_file, written only to a Kubernetes Secret."
  type        = string
  default     = ""
  nullable    = false
  sensitive   = true
  ephemeral   = true

  validation {
    condition = coalesce(var.config.public_endpoint, var.config.role == "control-plane") ? (
      trimspace(var.tunnel_private_key_pem) != ""
    ) : true
    error_message = "A public endpoint needs tunnel_private_key_pem from a protected caller file."
  }
}
