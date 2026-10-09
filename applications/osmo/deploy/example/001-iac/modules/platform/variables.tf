# =============================================================================
# Platform Module Variables
# =============================================================================

variable "parent_id" {
  description = "Nebius project ID"
  type        = string
}

variable "tenant_id" {
  description = "Nebius tenant ID (required for IAM group membership)"
  type        = string
}

variable "region" {
  description = "Nebius region"
  type        = string
}

variable "name_prefix" {
  description = "Prefix for resource names"
  type        = string
}

# -----------------------------------------------------------------------------
# Network Configuration (existing default network and subnet)
# -----------------------------------------------------------------------------

variable "network_id" {
  description = "Existing VPC network ID (set by nebius-env-init.sh)"
  type        = string
}

variable "subnet_id" {
  description = "Existing VPC subnet ID (set by nebius-env-init.sh)"
  type        = string
}

# -----------------------------------------------------------------------------
# Storage Configuration
# -----------------------------------------------------------------------------

variable "storage_bucket_name" {
  description = "Name for storage bucket"
  type        = string
}

# -----------------------------------------------------------------------------
# Filestore Configuration
# -----------------------------------------------------------------------------

variable "enable_filestore" {
  description = "Enable shared filesystem"
  type        = bool
  default     = true
}

variable "filestore_disk_type" {
  description = "Filestore disk type"
  type        = string
  default     = "NETWORK_SSD"
}

variable "filestore_size_gib" {
  description = "Filestore size in GiB"
  type        = number
  default     = 1024
}

variable "filestore_block_size_kib" {
  description = "Filestore block size in KiB"
  type        = number
  default     = 4
}

# -----------------------------------------------------------------------------
# PostgreSQL Configuration
# -----------------------------------------------------------------------------

variable "enable_managed_postgresql" {
  description = "Enable Nebius Managed PostgreSQL deployment"
  type        = bool
  default     = true
}

variable "postgresql_version" {
  description = "PostgreSQL version"
  type        = number
  default     = 16

  validation {
    condition     = contains([14, 15, 16], var.postgresql_version)
    error_message = "PostgreSQL version must be 14, 15, or 16."
  }
}

variable "postgresql_public_access" {
  description = "Enable public access to PostgreSQL (for testing only, not recommended for production)"
  type        = bool
  default     = false
}

variable "postgresql_platform" {
  description = "PostgreSQL platform (cpu-e2 for managed PostgreSQL in all regions)"
  type        = string
  default     = "cpu-e2"

  validation {
    condition     = contains(["cpu-d3", "cpu-e2"], var.postgresql_platform)
    error_message = "PostgreSQL platform must be cpu-e2 (recommended) or cpu-d3."
  }
}

variable "postgresql_preset" {
  description = "PostgreSQL resource preset (2vcpu-8gb is minimum)"
  type        = string
  default     = "2vcpu-8gb"
  
  validation {
    condition     = contains(["2vcpu-8gb", "4vcpu-16gb", "8vcpu-32gb"], var.postgresql_preset)
    error_message = "PostgreSQL preset must be 2vcpu-8gb, 4vcpu-16gb, or 8vcpu-32gb."
  }
}

variable "postgresql_disk_type" {
  description = "PostgreSQL disk type (network-ssd for managed PostgreSQL in all regions)"
  type        = string
  default     = "network-ssd"

  validation {
    condition     = contains(["nbs-csi-sc", "network-ssd"], var.postgresql_disk_type)
    error_message = "PostgreSQL disk type must be network-ssd (recommended) or nbs-csi-sc."
  }
}

variable "postgresql_disk_size_gib" {
  description = "PostgreSQL disk size in GiB"
  type        = number
  default     = 50
}

variable "postgresql_host_count" {
  description = "Number of PostgreSQL hosts"
  type        = number
  default     = 1
}

variable "postgresql_database_name" {
  description = "PostgreSQL database name"
  type        = string
  default     = "osmo"
}

variable "postgresql_username" {
  description = "PostgreSQL admin username"
  type        = string
  default     = "osmo_admin"
}

# -----------------------------------------------------------------------------
# Container Registry Configuration
# Reference: https://docs.nebius.com/terraform-provider/reference/resources/registry_v1_registry
# -----------------------------------------------------------------------------

variable "enable_container_registry" {
  description = "Enable Nebius Container Registry for storing container images"
  type        = bool
  default     = true
}

variable "container_registry_name" {
  description = "Custom name for container registry (defaults to <name_prefix>-registry)"
  type        = string
  default     = ""
}
