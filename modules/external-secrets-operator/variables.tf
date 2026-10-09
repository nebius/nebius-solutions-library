variable "namespace" {
  description = "Namespace where the External Secrets Operator will be installed."
  type        = string
  default     = "external-secrets"
}

variable "create_namespace" {
  description = "Whether to create the namespace used by the External Secrets Operator."
  type        = bool
  default     = true
}

variable "release_name" {
  description = "Helm release name for the External Secrets Operator."
  type        = string
  default     = "external-secrets"
}

variable "repository_url" {
  description = "Helm repository URL for the External Secrets Operator chart."
  type        = string
  default     = "https://charts.external-secrets.io"
}

variable "chart_name" {
  description = "Chart name for the External Secrets Operator release."
  type        = string
  default     = "external-secrets"
}

variable "chart_version" {
  description = "External Secrets Operator Helm chart version."
  type        = string
  default     = "2.2.0"
}

variable "install_crds" {
  description = "Whether the Helm release should install and manage the External Secrets CRDs."
  type        = bool
  default     = true
}

variable "atomic" {
  description = "Whether Helm should perform the release as an atomic operation."
  type        = bool
  default     = true
}

variable "wait" {
  description = "Whether Helm should wait until the External Secrets release is ready."
  type        = bool
  default     = true
}

variable "timeout_seconds" {
  description = "Timeout, in seconds, for the Helm release install or upgrade."
  type        = number
  default     = 300

  validation {
    condition     = var.timeout_seconds > 0
    error_message = "timeout_seconds must be greater than zero."
  }
}

variable "values" {
  description = "Additional Helm values documents to pass through to the External Secrets Operator chart."
  type        = list(string)
  default     = []
}
