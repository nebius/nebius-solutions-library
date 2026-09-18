variable "parent_id" {
  type        = string
  description = "Id of the folder where the resources going to be created."
  default     = null
}

variable "subnet_id" {
  type        = string
  description = "ID of the subnet."
  default     = null
}

variable "region" {
  type        = string
  description = "region"
  default     = "eu-north1"
}

variable "instance_name" {
  type        = string
  description = "name of the instance"
  default     = "instance"
}

variable "platform" {
  description = "VM platform."
  type        = string
  default     = "cpu-d3"
}

variable "preset" {
  description = "VM resources preset."
  type        = string
  default     = "16vcpu-64gb"
}

variable "cpu_nodes_preset" {
  description = "CPU and RAM configuration for instances."
  type        = string
  default     = null
}

variable "shared_filesystem_id" {
  description = "Id of an existing shared file system"
  type        = string
  default     = ""
}

variable "shared_filesystem_mount" {
  description = "mounting point of the shared file system"
  type        = string
  default     = "/mnt/share"
}

variable "users" {
  description = "List of users with their SSH keys"
  type = list(object({
    user_name      = string
    ssh_public_key = optional(string)                      # Inline SSH key
    ssh_key_path   = optional(string, "~/.ssh/id_rsa.pub") # Path to SSH key file
  }))
  default = []
  validation {
    condition = alltrue([
      for user in var.users : user.ssh_public_key != null || fileexists(user.ssh_key_path)
    ])
    error_message = "Each user must have at least one SSH key defined as 'ssh_public_key' or 'ssh_key_path'."
  }
}

variable "add_extra_storage" {
  type        = bool
  default     = false
  description = "if true, a new disk will be created and mounted at <extra_path>"
}

variable "extra_path" {
  type        = string
  default     = "/mnt/storage"
  description = "Folder where the network storage will be mounted on"
}

variable "boot_disk_size_gb" {
  type        = number
  default     = 50
  description = "size of the boot disk"
}

variable "extra_storage_size_gb" {
  type        = number
  default     = 50
  description = "size of the newly created nfs storage"
}

variable "extra_storage_class" {
  type        = string
  default     = "NETWORK_SSD"
  description = "Network type of additional disk being added"
}

variable "public_ip" {
  type        = bool
  default     = true
  description = "attach a public ip to the first created vm if true"
}

variable "create_public_ip_for_all_instances" {
  type        = bool
  default     = false
  description = "attach a public ip to all vms if true"
}

variable "mount_bucket" {
  type        = string
  description = "name of a bucket that should be mounted into fs"
  default     = ""
}

variable "s3_mount_path" {
  type        = string
  description = "mountpoint for s3 mount"
  default     = "/mnt/s3"
}
variable "aws_access_key_id" {
  type        = string
  description = "S3 access key"
  default     = ""
}

variable "aws_secret_access_key" {
  type        = string
  description = "S3 access key"
  default     = ""
}

variable "gpu_cluster" {
  type        = string
  description = "gpu cluster id"
  default     = ""
}
variable "preemptible" {
  description = "Whether the VM should be preemptible"
  type        = bool
  default     = false
}
