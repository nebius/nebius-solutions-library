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

variable "filestore_forbid_deletion" {
  description = "Protect Terraform-created Filestore from deletion."
  type        = bool
  default     = false
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

variable "node_group_strategy" {
  description = "Node-group rollout strategy on template changes. Set max_surge and max_unavailable using count or percent. GB300 requires max_surge to be zero."
  type = object({
    max_surge = optional(object({
      count   = optional(number)
      percent = optional(number)
    }))
    max_unavailable = optional(object({
      count   = optional(number)
      percent = optional(number)
    }))
  })
  default = null

  validation {
    condition = var.node_group_strategy == null || alltrue([
      for setting in [
        try(var.node_group_strategy.max_surge, null),
        try(var.node_group_strategy.max_unavailable, null),
      ] :
      setting == null || !(try(setting.count, null) != null && try(setting.percent, null) != null)
    ])
    error_message = "Set either count or percent for each node-group strategy setting, not both."
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

variable "gb300" {
  description = <<-EOT
    Number of production GB300 racks. Each rack creates one fixed 18-node MK8s
    node group and one 18-node NVLink instance group (72 GPUs per rack).
    boot_disk_size_gibibytes controls the network-backed boot disk size.
    Set local_nvme to true to pass through the host NVMe devices and combine
    them into kubelet ephemeral storage on each GB300 node.
    Set rack_count to zero to disable the GB300 path.
  EOT
  type = object({
    rack_count               = optional(number, 0)
    boot_disk_size_gibibytes = optional(number, 1024)
    local_nvme               = optional(bool, false)
  })
  default = {}

  validation {
    condition     = var.gb300.rack_count >= 0 && var.gb300.rack_count == floor(var.gb300.rack_count)
    error_message = "gb300.rack_count must be a non-negative whole number. Each rack always contains 18 nodes (72 GPUs)."
  }

  validation {
    condition     = var.gb300.boot_disk_size_gibibytes > 0 && var.gb300.boot_disk_size_gibibytes == floor(var.gb300.boot_disk_size_gibibytes)
    error_message = "gb300.boot_disk_size_gibibytes must be a positive whole number."
  }
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
  description = "InfiniBand fabric name. Leave null or empty to disable GPU clustering."
  type        = string
  default     = null
}

variable "gpu_nodes_public_ips" {
  description = "Assign public IP address to GPU nodes to make them directly accessible from the external internet."
  type        = bool
  default     = false
}

variable "enable_gpu_kubelet_numa" {
  description = "Enable platform-aware kubelet NUMA/topology configuration for supported GPU nodes."
  type        = bool
  default     = false
}

variable "gpu_kubelet_numa_config" {
  description = "Optional custom kubelet NUMA/topology configuration applied to GPU nodes via cloud-init. Overrides gpu_kubelet_numa_preset when set."
  type = object({
    cpu_manager_policy      = string
    topology_manager_policy = string
    memory_manager_policy   = string
    kube_reserved_memory    = optional(string)
    reserved_memory = list(object({
      numa_node = number
      memory    = string
    }))
  })
  default = null
}

variable "gpu_kubelet_numa_preset" {
  description = "Optional predefined kubelet NUMA/topology configuration for GPU nodes. Supported values: h200-standard, b200-standard, b300-standard. When null and enable_gpu_kubelet_numa is true, Terraform selects a preset from gpu_nodes_platform."
  type        = string
  default     = null

  validation {
    condition     = var.gpu_kubelet_numa_preset == null ? true : contains(["h200-standard", "b200-standard", "b300-standard"], var.gpu_kubelet_numa_preset)
    error_message = "gpu_kubelet_numa_preset must be null, \"h200-standard\", \"b200-standard\", or \"b300-standard\"."
  }
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

variable "kueue" {
  description = "Optional Kueue installation with topology-aware scheduling enabled by default; queue policy remains explicit."
  type = object({
    enabled                   = optional(bool, false)
    chart_version             = optional(string, "0.19.2")
    namespace                 = optional(string, "kueue-system")
    timeout_seconds           = optional(number, 300)
    topology_aware_scheduling = optional(bool, true)
    helm_values               = optional(list(string), [])
  })
  default = {}

  validation {
    condition     = can(regex("^[0-9]+\\.[0-9]+\\.[0-9]+$", var.kueue.chart_version))
    error_message = "kueue.chart_version must be a release version such as 0.19.2."
  }

  validation {
    condition = (
      length(var.kueue.namespace) >= 1 &&
      length(var.kueue.namespace) <= 63 &&
      can(regex("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", var.kueue.namespace))
    )
    error_message = "kueue.namespace must be a valid Kubernetes namespace name."
  }

  validation {
    condition     = var.kueue.timeout_seconds > 0
    error_message = "kueue.timeout_seconds must be greater than zero."
  }
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

variable "opa_gatekeeper_enable" {
  description = "Enable OPA Gatekeeper"
  type        = bool
  default     = false
}

variable "k8s_rbac_bindings" {
  description = "Optional Kubernetes RBAC bindings for Kubernetes cluster access. Disabled by default; set enabled = true only after the access model is approved."
  type = object({
    enabled = optional(bool, false)
    namespaces = optional(map(object({
      name        = optional(string)
      labels      = optional(map(string), {})
      annotations = optional(map(string), {})
    })), {})
    cluster_role_bindings = optional(map(object({
      name      = optional(string)
      role_name = string
      subjects = list(object({
        kind      = string
        name      = string
        api_group = optional(string)
        namespace = optional(string)
      }))
      labels      = optional(map(string), {})
      annotations = optional(map(string), {})
    })), {})
    namespace_role_bindings = optional(map(object({
      name      = optional(string)
      namespace = string
      role_kind = optional(string, "ClusterRole")
      role_name = string
      subjects = list(object({
        kind      = string
        name      = string
        api_group = optional(string)
        namespace = optional(string)
      }))
      labels      = optional(map(string), {})
      annotations = optional(map(string), {})
    })), {})
  })
  default = {}

  validation {
    condition = (
      !var.k8s_rbac_bindings.enabled ||
      length(var.k8s_rbac_bindings.cluster_role_bindings) +
      length(var.k8s_rbac_bindings.namespace_role_bindings) > 0
    )
    error_message = "When k8s_rbac_bindings.enabled is true, set at least one cluster_role_bindings or namespace_role_bindings entry."
  }
}

variable "binpacking_enable" {
  description = "Enable binpacking scheduler. Forced namespace mutation also requires OPA Gatekeeper."
  type        = bool
  default     = false
}

variable "binpacking_kube_sched_ver" {
  description = "Full kube-scheduler patch version to use for binpacking. If unset, it is inferred from k8s_version."
  type        = string
  default     = null

  validation {
    condition     = var.binpacking_kube_sched_ver == null || can(regex("^[0-9]+\\.[0-9]+\\.[0-9]+$", var.binpacking_kube_sched_ver))
    error_message = "binpacking_kube_sched_ver must be a full patch version like 1.34.9."
  }
}

variable "binpacking_forced_namespaces" {
  description = "If binpacking is enabled, force it for these namespaces instead of requiring each pod to opt in. Requires opa_gatekeeper_enable = true unless set to []."
  type        = list(string)
  default     = ["default"]
}
