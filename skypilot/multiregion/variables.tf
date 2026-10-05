# Global
variable "tenant_id" {
  description = "Tenant ID."
  type        = string
}

variable "parent_id" {
  description = "Project ID for single-region deployment (legacy)."
  type        = string
  default     = null
}

variable "subnet_id" {
  description = "Subnet ID for single-region deployment (legacy)."
  type        = string
  default     = null
}

variable "region" {
  description = "Region for single-region deployment (legacy)."
  type        = string
  default     = null
}

variable "parent_id_region1" {
  description = "Project ID for the first region."
  type        = string
  default     = null
}

variable "parent_id_region2" {
  description = "Project ID for the second region."
  type        = string
  default     = null
}

variable "subnet_id_region1" {
  description = "Subnet ID for the first region."
  type        = string
  default     = null
}

variable "subnet_id_region2" {
  description = "Subnet ID for the second region."
  type        = string
  default     = null
}

variable "region1" {
  description = "The first region."
  type        = string
  default     = null
}

variable "region2" {
  description = "The second region."
  type        = string
  default     = null

  validation {
    condition = (
      (
        var.region2 == null ||
        (
          var.parent_id_region2 != null &&
          var.subnet_id_region2 != null
        )
      ) &&
      (
        var.parent_id_region1 != null &&
        var.subnet_id_region1 != null &&
        var.region1 != null
      ) ||
      (
        var.parent_id != null &&
        var.subnet_id != null &&
        var.region != null
      )
    )
    error_message = "Provide region1 inputs (`parent_id_region1`, `subnet_id_region1`, `region1`) or legacy single-region inputs (`parent_id`, `subnet_id`, `region`)."
  }
}

variable "enable_secondary_region" {
  description = "Enable deployment of resources in the secondary region."
  type        = bool
  default     = false
}

# K8s cluster 

# Mk8s cluster name
variable "cluster_name" {
  description = "Base name used for MK8s cluster and related resources (node groups, service accounts)."
  type        = string
  default     = "infra"
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

variable "existing_filestore_region1" {
  description = "Existing Filestore ID for the first region."
  type        = string
  default     = null
}

variable "existing_filestore_region2" {
  description = "Existing Filestore ID for the second region."
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


# GPU node platform for primary region
variable "gpu_nodes_platform_primary" {
  description = "Platform for GPU nodes in the primary region."
  type        = string
  default     = null
}

# GPU node platform for secondary region
variable "gpu_nodes_platform_secondary" {
  description = "Platform for GPU nodes in the secondary region."
  type        = string
  default     = null
}

# Deprecated: use region-specific variables above
variable "gpu_nodes_platform" {
  description = "(Deprecated) Platform for nodes in the GPU node group. Use gpu_nodes_platform_primary and gpu_nodes_platform_secondary instead."
  type        = string
  default     = null
}

variable "gpu_nodes_driverfull_image" {
  description = "Use driver full images for GPU node gropus. Disabled GPU-Operator."
  type        = bool
  default     = false
}


# GPU node preset for primary region
variable "gpu_nodes_preset_primary" {
  description = "Preset for GPU nodes in the primary region."
  type        = string
  default     = null
}

# GPU node preset for secondary region
variable "gpu_nodes_preset_secondary" {
  description = "Preset for GPU nodes in the secondary region."
  type        = string
  default     = null
}

# Deprecated: use region-specific variables above
variable "gpu_nodes_preset" {
  description = "(Deprecated) Preset for GPU nodes. Use gpu_nodes_preset_primary and gpu_nodes_preset_secondary instead."
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
  description = "Infiniband's fabric name."
  type        = bool
  default     = true
}

# Infiniband fabric for primary region
variable "infiniband_fabric_primary" {
  description = "Infiniband fabric name for the primary region."
  type        = string
  default     = null
}

# Infiniband fabric for secondary region
variable "infiniband_fabric_secondary" {
  description = "Infiniband fabric name for the secondary region."
  type        = string
  default     = null
}

# Deprecated: use region-specific variables above
variable "infiniband_fabric" {
  description = "(Deprecated) Infiniband fabric name for all regions. Use infiniband_fabric_primary and infiniband_fabric_secondary instead."
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
    condition     = var.mig_parted_config == null || var.gpu_nodes_platform == null || contains(local.valid_mig_parted_configs[var.gpu_nodes_platform], var.mig_parted_config)
    error_message = "Invalid `mig_parted_config` for selected `gpu_nodes_platform`."
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

# Managed Service for MLflow
variable "enable_mlflow_cluster" {
  description = "Enable deployment of a Managed Service for MLflow cluster in the primary region."
  type        = bool
  default     = false
}

variable "mlflow_cluster_name" {
  description = "Name of the Managed Service for MLflow cluster."
  type        = string
  default     = null
}

variable "mlflow_cluster_description" {
  description = "Description of the Managed Service for MLflow cluster."
  type        = string
  default     = "Managed MLflow cluster for infra"
}

variable "mlflow_public_access" {
  description = "Whether MLflow cluster is publicly accessible."
  type        = bool
  default     = true
}

variable "mlflow_admin_username" {
  description = "MLflow admin username."
  type        = string
  default     = "admin"
}

variable "mlflow_admin_password" {
  description = "MLflow admin password. If null, a random password is generated."
  type        = string
  default     = null
  sensitive   = true

  validation {
    condition = (
      var.mlflow_admin_password == null ||
      (
        length(var.mlflow_admin_password) >= 8 &&
        length(var.mlflow_admin_password) <= 64 &&
        length(regexall("[A-Z]", var.mlflow_admin_password)) > 0 &&
        length(regexall("[a-z]", var.mlflow_admin_password)) > 0 &&
        length(regexall("[0-9]", var.mlflow_admin_password)) > 0 &&
        length(regexall("[-!@#$^&*_=+:;'\"\\\\|/?,.`~§±()\\[\\]{}<>]", var.mlflow_admin_password)) > 0
      )
    )
    error_message = "mlflow_admin_password must be 8-64 chars and include at least one uppercase, lowercase, digit, and special character."
  }
}

variable "mlflow_service_account_id" {
  description = "Service account ID used by MLflow to access object storage. If null, a dedicated service account is created."
  type        = string
  default     = null
}

variable "mlflow_size" {
  description = "MLflow cluster size. Leave null to use provider default."
  type        = string
  default     = null
}

variable "mlflow_storage_bucket_name" {
  description = "Optional bucket name for MLflow artifacts. If null, service creates one."
  type        = string
  default     = null
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

variable "gpu_health_cheker" {
  description = "Use preemptible VMs for GPU nodes"
  type        = bool
  default     = true
}
