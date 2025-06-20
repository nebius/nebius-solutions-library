# Global
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

variable "region" {
  description = "The current region."
  type        = string
}

# K8s cluster
variable "k8s_version" {
  description = "Kubernetes version to be used in the cluster."
  type        = string
  default     = "1.30"
}

variable "etcd_cluster_size" {
  description = "Size of etcd cluster. "
  type        = number
  default     = 3
}

variable "enable_egress_gateway" {
  description = "Enable Cilium Egress Gateway."
  type        = bool
  default     = false
}

# K8s filestore
variable "enable_filestore" {
  description = "Use Filestore."
  type        = bool
  default     = false
}

variable "filestore_disk_type" {
  description = "Filestore disk size in bytes."
  type        = string
  default     = "NETWORK_SSD"
}

variable "filestore_disk_size" {
  description = "Filestore disk size in bytes."
  type        = number
  default     = 1073741824
}

variable "filestore_block_size" {
  description = "Filestore block size in bytes."
  type        = number
  default     = 4096
}

# K8s access
variable "ssh_user_name" {
  description = "SSH username."
  type        = string
  default     = "ubuntu"
}

variable "ssh_public_key" {
  description = "SSH Public Key to access the cluster nodes"
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

# K8s CPU node group
variable "cpu_nodes_count" {
  description = "Number of nodes in the CPU-only node group."
  type        = number
  default     = 3
}

variable "cpu_nodes_platform" {
  description = "Platform for nodes in the CPU-only node group."
  type        = string
  default     = null
}

variable "cpu_nodes_preset" {
  description = "CPU and RAM configuration for nodes in the CPU-only node group."
  type        = string
  default     = null
}

variable "cpu_disk_type" {
  description = "Disk type for nodes in the CPU-only node group."
  type        = string
  default     = "NETWORK_SSD"
}

variable "cpu_disk_size" {
  description = "Disk size (in GB) for nodes in the CPU-only node group."
  type        = string
  default     = "128"
}

# K8s GPU node group
variable "gpu_nodes_count_per_group" {
  description = "Number of nodes in the GPU node group."
  type        = number
  default     = 2
}

variable "gpu_node_groups" {
  description = "Number of GPU node groups."
  type        = number
  default     = 1
}

variable "gpu_nodes_platform" {
  description = "Platform for nodes in the GPU node group."
  type        = string
  default     = null
}

variable "gpu_nodes_driverfull_image" {
  description = "Use driver full images for GPU node gropus. Disabled GPU-Operator."
  type        = bool
  default     = false
}

variable "gpu_nodes_preset" {
  description = "Configuration for GPU amount, CPU, and RAM for nodes in the GPU node group."
  type        = string
  default     = null
}

variable "gpu_disk_type" {
  description = "Disk type for nodes in the GPU node group."
  type        = string
  default     = "NETWORK_SSD" # NETWORK_SSD NETWORK_SSD_NON_REPLICATED NETWORK_SSD_IO_M3
}

variable "gpu_disk_size" {
  description = "Disk size (in GB) for nodes in the GPU node group."
  type        = string
  default     = "1023"
}

variable "infiniband_fabric" {
  description = "Infiniband's fabric name."
  type        = string
  default     = null
}

variable "gpu_nodes_assign_public_ip" {
  description = "Assign public IP address to GPU nodes to make them directly accessible from the external internet."
  type        = bool
  default     = false
}

variable "enable_k8s_node_group_sa" {
  description = "Enable K8S Node Group Service Account"
  type        = bool
  default     = true
}

variable "mig_parted_config" {
  description = "MIG partition config to be assigned to node group label"
  type        = string
  default     = null

  validation {
    condition     = var.mig_parted_config == null || contains(local.valid_mig_parted_configs[local.gpu_nodes_platform], coalesce(var.mig_parted_config, "null"))
    error_message = "Invalid MIG config '${coalesce(var.mig_parted_config, "null")}' for the selected GPU platform '${local.gpu_nodes_platform}'. Must be one of ${join(", ", local.valid_mig_parted_configs[local.gpu_nodes_platform])} or left unset."
  }
}

variable "egress_nodes_preset" {
  description = "Preset for egress nodes."
  type        = string
  default     = "4vcpu-16gb"
}

# Observability
variable "enable_loki" {
  description = "Enable Loki for logs aggregation."
  type        = bool
  default     = true
}

variable "enable_prometheus" {
  description = "Enable Prometheus for metrics collection."
  type        = bool
  default     = true
}

variable "loki_access_key_id" {
  type    = string
  default = null
}

variable "loki_secret_key" {
  type    = string
  default = null
}

variable "loki_custom_replication_factor" {
  description = "By default there will be one replica of Loki for each 20 nodes in the cluster. Configure this variable if you want to set number of replicas manually"
  type        = number
  default     = null
}

# Helm
variable "iam_token" {
  description = "Token for Helm provider authentication. (source environment.sh)"
  type        = string
}

variable "test_mode" {
  description = "Switch between real usage and testing"
  type        = bool
  default     = false
}

variable "enable_kuberay" {
  description = "Enable kuberay"
  type        = bool
  default     = false
}

variable "kuberay_min_gpu_replicas" {
  description = "Minimum amount of kuberay gpu worker pods"
  type        = number
  default     = 0
}

variable "kuberay_max_gpu_replicas" {
  description = "Minimum amount of kuberay gpu worker pods"
  type        = number
  default     = 1
}

variable "mig_strategy" {
  description = "MIG strategy for GPU operator"
  type        = string
  default     = null
}
