variable "region" {
  description = "The current region."
  type        = string
}

variable "vpc_name" {
  description   = "Name for Network"
  type          = string
  default       = "tf-default-network"
}

variable "parent_id" {
  description   = "Parent id"
  type          = string
}

variable "gateway_subnet_cidr" {
  description   = "CIDR for the gateway subnet"
  type          = string
  default       = null
}

variable "private_subnet_cidr" {
  description   = "CIDR for the private subnet"
  type          = string
  default       = null
}

# SSH access
variable "ssh_user_name" {
  description = "SSH username."
  type        = string
  default     = "gateway"
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