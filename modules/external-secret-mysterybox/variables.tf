variable "namespace" {
  description = "Namespace where the ExternalSecret target Secret will be created."
  type        = string
}

variable "create_namespace" {
  description = "Whether to create the namespace used by the ExternalSecret and, when applicable, the SecretStore."
  type        = bool
  default     = false
}

variable "secret_store_kind" {
  description = "Kind of External Secrets store reference to use."
  type        = string
  default     = "SecretStore"

  validation {
    condition     = contains(["SecretStore", "ClusterSecretStore"], var.secret_store_kind)
    error_message = "secret_store_kind must be SecretStore or ClusterSecretStore."
  }
}

variable "secret_store_name" {
  description = "Name of the SecretStore or ClusterSecretStore used by the ExternalSecret."
  type        = string
}

variable "create_secret_store" {
  description = "Whether to create the referenced SecretStore or ClusterSecretStore."
  type        = bool
  default     = true
}

variable "api_domain" {
  description = "Nebius API domain used by the External Secrets MysteryBox provider."
  type        = string
  default     = "api.nebius.cloud:443"
}

variable "service_account_credentials_secret_name" {
  description = "Name of the Kubernetes Secret that stores Nebius service account credentials in Subject Credentials JSON format. Required when create_secret_store is true."
  type        = string
  default     = null
  nullable    = true
}

variable "service_account_credentials_secret_key" {
  description = "Key inside the Kubernetes Secret that stores the Nebius service account credentials JSON. Required when create_secret_store is true."
  type        = string
  default     = null
  nullable    = true
}

variable "ca_provider_secret_name" {
  description = "Optional Secret name that holds a custom CA certificate for the MysteryBox API endpoint."
  type        = string
  default     = null
  nullable    = true
}

variable "ca_provider_secret_key" {
  description = "Key inside the custom CA Secret that contains the certificate. Required when ca_provider_secret_name is set."
  type        = string
  default     = "ca.crt"
}

variable "external_secret_name" {
  description = "Optional name for the ExternalSecret resource. Defaults to target_secret_name."
  type        = string
  default     = null
  nullable    = true
}

variable "target_secret_name" {
  description = "Name of the Kubernetes Secret that External Secrets should keep reconciled from MysteryBox."
  type        = string
}

variable "mysterybox_secret_id" {
  description = "Nebius MysteryBox secret ID to extract into the target Kubernetes Secret."
  type        = string
}

variable "mysterybox_secret_version" {
  description = "Optional MysteryBox secret version. If unset, External Secrets uses the primary version."
  type        = string
  default     = null
  nullable    = true
}

variable "refresh_interval" {
  description = "How often External Secrets should refresh the target Secret from MysteryBox."
  type        = string
  default     = "1h"
}

variable "creation_policy" {
  description = "External Secrets target creation policy."
  type        = string
  default     = "Owner"

  validation {
    condition     = contains(["Owner", "Orphan", "Merge", "None"], var.creation_policy)
    error_message = "creation_policy must be Owner, Orphan, Merge, or None."
  }
}

variable "deletion_policy" {
  description = "Optional External Secrets target deletion policy."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition = (
      var.deletion_policy == null ||
      contains(["Delete", "Merge", "Retain"], var.deletion_policy)
    )
    error_message = "deletion_policy must be null, Delete, Merge, or Retain."
  }
}
