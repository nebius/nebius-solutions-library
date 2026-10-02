variable "tenant_id" {
  description = "Tenant ID."
  type        = string
}

variable "parent_id" {
  description = "Project ID."
  type        = string
}

variable "subnet_id" {
  description = "Subnet ID."
  type        = string
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
# Access By IP
variable "public_ip_allocation_id" {
  description = "Id of a manually created public_ip_allocation."
  type        = string
  default     = null
}
variable "tailscale" {
  type = object({
    auth_mysterybox_secret_id = string
    relay_server_port         = optional(number, 40000)
    instance_count            = optional(number, 1)
    instance_preset           = optional(string, "4vcpu-16gb")
    version                   = string
  })
  validation {
    condition     = trimspace(var.tailscale.version) != ""
    error_message = "`tailscale.version` is required and cannot be empty."
  }
  validation {
    condition     = trimspace(var.tailscale.auth_mysterybox_secret_id) != ""
    error_message = "`tailscale.auth_mysterybox_secret_id` is required and cannot be empty."
  }
}

variable "test_mode" {
  description = "Switch between real usage and testing."
  type        = bool
  default     = false
}
