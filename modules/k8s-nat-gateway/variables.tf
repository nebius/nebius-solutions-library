variable "name" {
  description = "Name prefix for the NAT gateway resources."
  type        = string
}

variable "project_id" {
  description = "Project containing the cluster and subnets."
  type        = string
}

variable "subnet_id" {
  description = "Existing worker subnet. Its VPC is reused; its routing is not modified."
  type        = string
}

variable "cluster_subnet_id" {
  description = "Cluster control-plane subnet used by the Service controller for its internal LB listener. May equal subnet_id. Gateway nodes remain in their own subnet."
  type        = string
}

variable "mk8s_cluster_id" {
  description = "Cluster in which to create the dedicated gateway node group."
  type        = string
}

variable "mk8s_version" {
  description = "Kubernetes version; null uses the backend default."
  type        = string
  default     = null
}

variable "node_count" {
  description = "Fixed gateway node count. A single node has no redundancy."
  type        = number
  default     = 2
  nullable    = false

  validation {
    condition     = var.node_count >= 1 && floor(var.node_count) == var.node_count
    error_message = "node_count must be a positive whole number."
  }
}

variable "public_ip_pool_size" {
  description = "Reserved public /32 addresses, including one additional address for rolling upgrades. Keep this pool stable for allowlisting."
  type        = number
  default     = 3
  nullable    = false

  validation {
    condition     = var.public_ip_pool_size >= 2 && var.public_ip_pool_size <= 1024 && floor(var.public_ip_pool_size) == var.public_ip_pool_size
    error_message = "public_ip_pool_size must be a whole number between 2 and 1024."
  }
}

variable "nodes_platform" {
  description = "CPU platform for the gateway nodes."
  type        = string
  default     = "cpu-d3"
}

variable "nodes_preset" {
  description = "CPU and RAM preset for the gateway nodes."
  type        = string
  default     = "4vcpu-16gb"
}

variable "nodes_disk_type" {
  description = "Boot disk type for the gateway nodes."
  type        = string
  default     = "NETWORK_SSD"
}

variable "nodes_disk_size" {
  description = "Boot disk size in GiB."
  type        = number
  default     = 128
}

variable "ssh_user_name" {
  description = "SSH user configured through the common Kubernetes cloud-init template."
  type        = string
}

variable "ssh_public_key" {
  description = "Public SSH key for the gateway nodes."
  type        = string
}
