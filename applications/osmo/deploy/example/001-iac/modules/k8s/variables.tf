# =============================================================================
# Kubernetes Module Variables
# =============================================================================

variable "parent_id" {
  description = "Nebius project ID"
  type        = string
}

variable "tenant_id" {
  description = "Nebius tenant ID"
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
  description = "Subnet ID for the cluster"
  type        = string
}

# -----------------------------------------------------------------------------
# Cluster Configuration
# -----------------------------------------------------------------------------

variable "k8s_version" {
  description = "Kubernetes version"
  type        = string
  default     = null
}

variable "etcd_cluster_size" {
  description = "Size of etcd cluster"
  type        = number
  default     = 3
}

variable "enable_public_endpoint" {
  description = "Enable public endpoint for Kubernetes API"
  type        = bool
  default     = true
}

# -----------------------------------------------------------------------------
# SSH Access
# -----------------------------------------------------------------------------

variable "ssh_user_name" {
  description = "SSH username for node access"
  type        = string
  default     = "ubuntu"
}

variable "ssh_public_key" {
  description = "SSH public key for node access"
  type        = string
}

# -----------------------------------------------------------------------------
# CPU Node Group Configuration
# -----------------------------------------------------------------------------

variable "cpu_nodes_count" {
  description = "Number of CPU nodes"
  type        = number
  default     = 3
}

variable "cpu_nodes_platform" {
  description = "Platform for CPU nodes"
  type        = string
  default     = "cpu-d3"
}

variable "cpu_nodes_preset" {
  description = "Resource preset for CPU nodes"
  type        = string
  default     = "16vcpu-64gb"
}

variable "cpu_disk_type" {
  description = "Disk type for CPU nodes"
  type        = string
  default     = "NETWORK_SSD"
}

variable "cpu_disk_size_gib" {
  description = "Disk size in GiB for CPU nodes"
  type        = number
  default     = 128
}

variable "cpu_nodes_assign_public_ip" {
  description = "Assign public IPs to CPU nodes"
  type        = bool
  default     = true
}

# -----------------------------------------------------------------------------
# GPU Node Group Configuration
# -----------------------------------------------------------------------------

variable "gpu_nodes_count_per_group" {
  description = "Number of GPU nodes per group"
  type        = number
  default     = 1
}

variable "gpu_node_groups" {
  description = "Number of GPU node groups"
  type        = number
  default     = 1
}

variable "gpu_nodes_platform" {
  description = "Platform for GPU nodes"
  type        = string
}

variable "gpu_nodes_preset" {
  description = "Resource preset for GPU nodes"
  type        = string
}

variable "gpu_disk_type" {
  description = "Disk type for GPU nodes"
  type        = string
  default     = "NETWORK_SSD"
}

variable "gpu_disk_size_gib" {
  description = "Disk size in GiB for GPU nodes"
  type        = number
  default     = 1023
}

variable "gpu_nodes_assign_public_ip" {
  description = "Assign public IPs to GPU nodes"
  type        = bool
  default     = false
}

variable "enable_gpu_cluster" {
  description = "Enable GPU cluster with InfiniBand"
  type        = bool
  default     = true
}

variable "infiniband_fabric" {
  description = "InfiniBand fabric name"
  type        = string
}

variable "enable_gpu_taints" {
  description = "Add NoSchedule taint to GPU nodes"
  type        = bool
  default     = true
}

variable "gpu_nodes_preemptible" {
  description = "Use preemptible GPU nodes"
  type        = bool
  default     = false
}

variable "gpu_reservation_ids" {
  description = "List of capacity block group IDs for GPU reservations"
  type        = list(string)
  default     = []
}

variable "gpu_nodes_driverfull_image" {
  description = "Use Nebius driverfull images with pre-installed NVIDIA drivers"
  type        = bool
  default     = false
}

variable "gpu_drivers_preset" {
  description = "CUDA driver preset for driverfull images (e.g. cuda12, cuda12.8, cuda13.0)"
  type        = string
  default     = "cuda12"
}

# -----------------------------------------------------------------------------
# Filestore Configuration
# -----------------------------------------------------------------------------

variable "enable_filestore" {
  description = "Enable filestore attachment"
  type        = bool
  default     = true
}

variable "filestore_id" {
  description = "Filestore ID to attach"
  type        = string
  default     = null
}
