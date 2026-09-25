# Global parameters
variable "parent_id" {
  description = "Project ID."
  type        = string
}

variable "subnet_id" {
  description = "Subnet ID."
  type        = string
}

variable "region" {
  description = "Project region."
  type        = string
  default     = "eu-west2" # https://docs.nebius.com/overview/regions
}

# Platform
variable "platform" {
  description = "Platform for DSVM host."
  type        = string
  default     = null
}

variable "preset" {
  description = "Preset for DSVM host."
  type        = string
  default     = null
}

variable "boot_image_family" {
  description = "Boot image family for the DSVM host."
  type        = string
  default     = null
}

# SSH access
variable "ssh_user_name" {
  description = "SSH username."
  type        = string
  default     = "ubuntu"
}

variable "ssh_public_key" {
  description = "SSH Public Key to access the cluster nodes."
  type = object({
    key  = optional(string),
    path = optional(string, "~/.ssh/id_rsa.pub")
  })
  default = {}
  validation {
    condition     = var.ssh_public_key.key != null || fileexists(var.ssh_public_key.path)
    error_message = "SSH Public Key must be set by `key` or file `path` ${var.ssh_public_key.path}"
  }
}

variable "test_mode" {
  description = "Switch between real usage and testing."
  type        = bool
  default     = false
}
