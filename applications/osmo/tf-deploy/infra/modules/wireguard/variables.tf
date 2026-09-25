# =============================================================================
# WireGuard Module Variables
# =============================================================================

variable "parent_id" {
  description = "Nebius project ID"
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
# Network Configuration
# -----------------------------------------------------------------------------

variable "subnet_id" {
  description = "Subnet ID for the instance"
  type        = string
}

variable "vpc_cidr" {
  description = "VPC CIDR for routing (unused, kept for future use)"
  type        = string
  default     = ""
}

variable "wg_network" {
  description = "WireGuard VPN network CIDR"
  type        = string
  default     = "10.8.0.0/24"
}

# -----------------------------------------------------------------------------
# Instance Configuration
# -----------------------------------------------------------------------------

variable "platform" {
  description = "VM platform (cpu-d3 available in all regions, cpu-e2 only in eu-north1)"
  type        = string
  default     = "cpu-d3"
}

variable "preset" {
  description = "VM resource preset"
  type        = string
  default     = "4vcpu-16gb"
}

variable "disk_size_gib" {
  description = "Boot disk size in GiB"
  type        = number
  default     = 64
}

# -----------------------------------------------------------------------------
# WireGuard Configuration
# -----------------------------------------------------------------------------

variable "wg_port" {
  description = "WireGuard UDP port"
  type        = number
  default     = 51820
}

variable "ui_port" {
  description = "WireGuard Web UI port"
  type        = number
  default     = 5000
}

# -----------------------------------------------------------------------------
# SSH Access
# -----------------------------------------------------------------------------

variable "ssh_user_name" {
  description = "SSH username"
  type        = string
  default     = "ubuntu"
}

variable "ssh_public_key" {
  description = "SSH public key"
  type        = string
}
