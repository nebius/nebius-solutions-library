variable "mk8s_cluster_id" {
  description = "Managed Kubernetes Cluster ID"
  type        = string
}

variable "mk8s_version" {
  description = "Managed Kubernetes Cluster Version"
  type        = string
}

variable "nodes_platform" {
  description = "Platform for nodes in the node group"
  type        = string
  default     = "cpu-d3"
}

variable "nodes_preset" {
  description = "CPU and RAM configuration for nodes in the node group"
  type        = string
  default     = "4vcpu-16gb"
}

variable "nodes_disk_type" {
  description = "Disk type for nodes in the node group"
  type        = string
  default     = "NETWORK_SSD"
}

variable "nodes_disk_size" {
  description = "Disk size (in GB) for nodes in the node group"
  type        = string
  default     = "128"
}

variable "project_id" {
  description = "Project ID"
  type        = string
}

variable "ssh_public_key" {
  description = "SSH public key to access the nodes"
  type        = string
}

variable "ssh_user_name" {
  description = "SSH username"
  type        = string
}

variable "subnet_id" {
  description = "Subnet ID"
  type        = string
}