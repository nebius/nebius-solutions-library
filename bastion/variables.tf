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
  default     = "bastion"
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

variable "ssh_private_key_path" {
  description = "Optional private SSH key path used by Terraform test_mode remote checks."
  type        = string
  default     = null
}

# Access By IP
variable "public_ip_allocation_id" {
  description = "Id of a manually created public_ip_allocation."
  type        = string
  default     = null
}

variable "enable_bastion_security_group" {
  description = "Create and attach a managed security group for the bastion instance."
  type        = bool
  default     = true
}

variable "bastion_allowed_ssh_cidrs" {
  description = "CIDR blocks allowed to connect to the bastion over SSH. When null or empty, no managed SSH ingress rule is created."
  type        = list(string)
  default     = null

  validation {
    condition = alltrue([
      for cidr in(var.bastion_allowed_ssh_cidrs != null ? var.bastion_allowed_ssh_cidrs : []) :
      can(cidrhost(cidr, 0)) && can(regex("^((25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\\.){3}(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])/([0-9]|[12][0-9]|3[0-2])$", cidr))
    ])
    error_message = "bastion_allowed_ssh_cidrs must contain valid IPv4 CIDR blocks."
  }
}

variable "bastion_allowed_wireguard_cidrs" {
  description = "CIDR blocks allowed to connect to WireGuard UDP 51820. Defaults to bastion_allowed_ssh_cidrs when null."
  type        = list(string)
  default     = null

  validation {
    condition = alltrue([
      for cidr in(var.bastion_allowed_wireguard_cidrs != null ? var.bastion_allowed_wireguard_cidrs : []) :
      can(cidrhost(cidr, 0)) && can(regex("^((25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\\.){3}(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])/([0-9]|[12][0-9]|3[0-2])$", cidr))
    ])
    error_message = "bastion_allowed_wireguard_cidrs must contain valid IPv4 CIDR blocks."
  }
}

variable "bastion_allowed_wireguard_ui_cidrs" {
  description = "CIDR blocks allowed to connect to WireGuard UI TCP 5000. Defaults to no public UI access when null."
  type        = list(string)
  default     = null

  validation {
    condition = alltrue([
      for cidr in(var.bastion_allowed_wireguard_ui_cidrs != null ? var.bastion_allowed_wireguard_ui_cidrs : []) :
      can(cidrhost(cidr, 0)) && can(regex("^((25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\\.){3}(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])/([0-9]|[12][0-9]|3[0-2])$", cidr))
    ])
    error_message = "bastion_allowed_wireguard_ui_cidrs must contain valid IPv4 CIDR blocks."
  }
}

variable "bastion_allow_unrestricted_ingress_rules" {
  description = "Allow 0.0.0.0/0 or empty-source ALLOW ingress rules in the managed bastion security group."
  type        = bool
  default     = false
}

variable "bastion_security_group_name" {
  description = "Name of the managed bastion security group."
  type        = string
  default     = "bastion-security-group"

  validation {
    condition     = length(trimspace(var.bastion_security_group_name)) > 0
    error_message = "bastion_security_group_name must not be empty."
  }
}

variable "bastion_egress_cidrs" {
  description = "CIDR blocks the bastion can reach. Defaults to unrestricted egress for package installs and Nebius API access."
  type        = list(string)
  default     = ["0.0.0.0/0"]

  validation {
    condition = alltrue([
      for cidr in var.bastion_egress_cidrs :
      can(cidrhost(cidr, 0)) && can(regex("^((25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\\.){3}(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])/([0-9]|[12][0-9]|3[0-2])$", cidr))
    ])
    error_message = "bastion_egress_cidrs must contain valid IPv4 CIDR blocks."
  }
}

variable "bastion_extra_security_group_ids" {
  description = "Additional existing security group IDs to attach to the bastion network interface."
  type        = list(string)
  default     = []
}

variable "bastion_extra_ingress_rules" {
  description = "Additional ingress rules for the managed bastion security group."
  type = map(object({
    name                     = optional(string)
    access                   = optional(string, "ALLOW")
    protocol                 = optional(string, "TCP")
    rule_type                = optional(string, "STATEFUL")
    priority                 = optional(number, 500)
    source_cidrs             = optional(list(string), [])
    source_security_group_id = optional(string)
    destination_ports        = optional(list(number), [])
    labels                   = optional(map(string), {})
  }))
  default = {}
}

variable "bastion_extra_egress_rules" {
  description = "Additional egress rules for the managed bastion security group."
  type = map(object({
    name                          = optional(string)
    access                        = optional(string, "ALLOW")
    protocol                      = optional(string, "ANY")
    rule_type                     = optional(string, "STATEFUL")
    priority                      = optional(number, 900)
    destination_cidrs             = optional(list(string), [])
    destination_security_group_id = optional(string)
    destination_ports             = optional(list(number), [])
    labels                        = optional(map(string), {})
  }))
  default = {}
}

variable "test_mode" {
  description = "Switch between real usage and testing."
  type        = bool
  default     = false
}
