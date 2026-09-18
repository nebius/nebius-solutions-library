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

# Mk8s cluster name
variable "cluster_name" {
  description = "Base name used for MK8s cluster and related resources (node groups, service accounts)."
  type        = string
  default     = "k8s-training"
}

variable "k8s_version" {
  description = "Kubernetes version to be used in the cluster. Leave null to use backend default (recommended), or choose 1.31 or above."
  type        = string
  default     = null
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

variable "existing_filestore" {
  description = "Add existing SFS"
  type        = string
  default     = null
}

variable "filestore_disk_type" {
  description = "Filestore disk size in bytes."
  type        = string
  default     = "NETWORK_SSD"
}

variable "filestore_disk_size_gibibytes" {
  description = "Filestore disk size in bytes."
  type        = number
  default     = 1 # 1 GiB
}

variable "filestore_block_size_kibibytes" {
  description = "Filestore block size in bytes."
  type        = number
  default     = 4 # 4kb
}

variable "filestore_mount_path" {
  description = "Mount path for the shared filesystem on Kubernetes nodes."
  type        = string
  default     = "/mnt/data"
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
variable "cpu_nodes_fixed_count" {
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
variable "gpu_nodes_fixed_count_per_group" {
  description = "Number of nodes in the GPU node group."
  type        = number
  default     = 2
}

variable "gpu_nodes_autoscaling" {
  type = object({
    enabled  = optional(bool, false)
    min_size = optional(number)
    max_size = optional(number)
  })
  default = {}
}

variable "cpu_nodes_autoscaling" {
  type = object({
    enabled  = optional(bool, false)
    min_size = optional(number)
    max_size = optional(number)
  })
  default = {}
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

variable "enable_gpu_cluster" {
  description = "Enable GPU clustering and InfiniBand for the GPU node group."
  type        = bool
  default     = true

  validation {
    condition     = !var.enable_gpu_cluster || startswith(local.gpu_nodes_preset, "8gpu-")
    error_message = "GPU clustering requires an 8-GPU preset. Set 'enable_gpu_cluster = false' for single-GPU presets such as '${local.gpu_nodes_preset}'."
  }
}

variable "infiniband_fabric" {
  description = "Infiniband's fabric name."
  type        = string
  default     = null
}

variable "gpu_nodes_public_ips" {
  description = "Assign public IP address to GPU nodes to make them directly accessible from the external internet."
  type        = bool
  default     = false
}

variable "cpu_nodes_public_ips" {
  description = "Assign public IP address to CPU nodes to make them directly accessible from the external internet."
  type        = bool
  default     = false
}

variable "mk8s_cluster_public_endpoint" {
  description = "Assign public endpoint to MK8S cluster to make it directly accessible from the external internet."
  type        = bool
  default     = true
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
    condition = var.mig_parted_config == null ? true : contains(
      lookup(local.valid_mig_parted_configs, local.gpu_nodes_platform, []),
      var.mig_parted_config,
    )
    error_message = length(lookup(local.valid_mig_parted_configs, local.gpu_nodes_platform, [])) > 0 ? "Invalid MIG config '${coalesce(var.mig_parted_config, "null")}' for the selected GPU platform '${local.gpu_nodes_platform}'. Must be one of ${join(", ", lookup(local.valid_mig_parted_configs, local.gpu_nodes_platform, []))} or left unset." : "GPU platform '${local.gpu_nodes_platform}' does not support MIG partitioning. Leave 'mig_parted_config' unset."
  }
}

variable "gpu_enable_local_disks" {
  description = "Whether to request local NVMe passthrough disks and use them as managed kubelet ephemeral storage"
  type        = bool
  default     = false

  validation {
    condition = (
      !var.gpu_enable_local_disks ||
      (
        local.gpu_nodes_platform == "gpu-b300-sxm" &&
        local.gpu_nodes_preset == "8gpu-192vcpu-2768gb"
      )
    )
    error_message = "Local disks are supported only on B300 platform with preset 8gpu-192vcpu-2768gb."
  }
}


# Observability

variable "enable_nebius_o11y_agent" {
  description = "Enable Nebius Observability Agent for Kubernetes [marketplace/nebius/nebius-observability-agent]"
  type        = bool
  default     = true
}

variable "collectK8sClusterMetrics" {
  description = "Enable collection of Kubernetes cluster metrics in Nebius Observability Agent"
  type        = bool
  default     = false
}

variable "enable_grafana" {
  description = "Enable Grafana [marketplace/nebius/grafana-solution-by-nebius]"
  type        = bool
  default     = true
}

variable "loki" {
  type = object({
    enabled            = optional(bool, false)
    region             = optional(string)
    replication_factor = optional(number)
  })
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

variable "enable_kuberay_cluster" {
  description = "Enable kuberay and deploy RayCluster"
  type        = bool
  default     = false
}

variable "enable_kuberay_service" {
  description = "Enable kuberay and deploy RayService"
  type        = bool
  default     = false
}

variable "kuberay_cpu_worker_image" {
  description = "Docker image to use for CPU worker pods"
  default     = null
}

variable "kuberay_min_cpu_replicas" {
  description = "Minimum amount of kuberay CPU worker pods"
  type        = number
  default     = 0
}

variable "kuberay_max_cpu_replicas" {
  description = "Minimum amount of kuberay CPU worker pods"
  type        = number
  default     = 0
}

variable "kuberay_cpu_resources" {
  description = "Resources given to each CPU worker pod"
  type = object({
    cpus   = number
    memory = number
  })
  default = null
}

#gpu worker pod setup
variable "kuberay_gpu_worker_image" {
  description = "Docker image to use for GPU worker pods"
  default     = null
}
variable "kuberay_min_gpu_replicas" {
  description = "Minimum amount of kuberay GPU worker pods"
  type        = number
  default     = 0
}

variable "kuberay_max_gpu_replicas" {
  description = "Minimum amount of kuberay GPU worker pods"
  type        = number
  default     = 0
}

variable "kuberay_gpu_resources" {
  description = "Resources given to each GPU worker pod"
  type = object({
    cpus   = number
    gpus   = number
    memory = number
  })
  default = null
}

variable "kuberay_serve_config_v2" {
  description = "Represents the configuration that Ray Serve uses to deploy the application"
  type        = string
  default     = null
}

variable "mig_strategy" {
  description = "MIG strategy for GPU operator"
  type        = string
  default     = null
}

variable "cpu_nodes_preemptible" {
  description = "Whether the cpu nodes should be preemptible"
  type        = bool
  default     = false
}

variable "gpu_nodes_preemptible" {
  description = "Use preemptible VMs for GPU nodes"
  type        = bool
  default     = false
}

variable "custom_driver" {
  description = "Use customized driver for the GPU Operator, e.g. to run Cuda 13 on H200"
  type        = bool
  default     = false

  validation {
    condition     = !(var.custom_driver && var.gpu_nodes_driverfull_image)
    error_message = "You cannot enable both 'custom_driver' and 'gpu_nodes_driverfull_image' at the same time."
  }

}

variable "filesystem_csi" {
  description = "Configuration for Nebius Shared Filesystem CSI installation when a shared filesystem is present. Set previous_default_storage_class_name to an empty string to skip demoting another StorageClass."
  type = object({
    chart_version                       = optional(string, "0.1.5")
    namespace                           = optional(string, "kube-system")
    make_default_storage_class          = optional(bool, true)
    previous_default_storage_class_name = optional(string, "compute-csi-default-sc")
  })
  default = {}
}

variable "enable_opa_gatekeeper" {
  description = "Enable OPA Gatekeeper"
  type        = bool
  default     = false
}
