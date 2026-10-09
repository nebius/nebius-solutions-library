variable "parent_id" {
  description = "Project ID."
  type        = string
}

variable "vpc_network_id" {
  description = "Existing VPC network ID where the NAT gateway subnets and route table will be created."
  type        = string
  nullable    = false

  validation {
    condition     = trimspace(var.vpc_network_id) != "" && can(regex("^vpcnetwork-", trimspace(var.vpc_network_id)))
    error_message = "vpc_network_id must be set to an existing VPC network ID like vpcnetwork-...."
  }
}

variable "name_prefix" {
  description = "Prefix used for resource names."
  type        = string
  default     = "custom-nat-gateway"
}

variable "ssh_user_name" {
  description = "SSH username used on both virtual machines."
  type        = string
  default     = "ubuntu"
}

variable "ssh_public_key" {
  description = "SSH public key used to access the virtual machines."
  type = object({
    key  = optional(string),
    path = optional(string, "~/.ssh/id_ed25519.pub")
  })
  default = {}

  validation {
    condition     = var.ssh_public_key.key != null || fileexists(pathexpand(var.ssh_public_key.path))
    error_message = "SSH public key must be set by `key` or by file `path`."
  }
}

variable "source_image_family" {
  description = "Image family used for both virtual machines."
  type        = string
  default     = "ubuntu24.04-driverless"
}

variable "gateway_platform" {
  description = "Platform used by the NAT gateway VM."
  type        = string
  default     = "cpu-d3"
}

variable "gateway_preset" {
  description = "Preset used by the NAT gateway VM."
  type        = string
  default     = "4vcpu-16gb"
}

variable "gateway_boot_disk_size_gib" {
  description = "Gateway VM boot disk size in GiB."
  type        = number
  default     = 50
}

variable "workload_platform" {
  description = "Platform used by the private test VM."
  type        = string
  default     = "cpu-d3"
}

variable "workload_preset" {
  description = "Preset used by the private test VM."
  type        = string
  default     = "4vcpu-16gb"
}

variable "workload_boot_disk_size_gib" {
  description = "Private test VM boot disk size in GiB."
  type        = number
  default     = 30
}

variable "deploy_test_vm" {
  description = "When true, deploy the private test VM and its private allocation."
  type        = bool
  default     = true
}
