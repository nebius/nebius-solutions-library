# region Cloud

variable "region" {
  description = "Region of the project."
  type        = string
  nullable    = false
}
resource "terraform_data" "check_region" {
  lifecycle {
    precondition {
      condition     = contains(module.resources.regions, var.region)
      error_message = "Unknown region '${var.region}'. See https://docs.nebius.com/overview/regions"
    }
  }
}

variable "iam_project_id" {
  description = "ID of the IAM project."
  type        = string
  nullable    = false

  validation {
    condition     = startswith(var.iam_project_id, "project-")
    error_message = "ID of the IAM project must start with `project-`."
  }
}
data "nebius_iam_v1_project" "this" {
  id = var.iam_project_id
}

variable "iam_tenant_id" {
  description = "ID of the IAM tenant."
  type        = string
  nullable    = false

  validation {
    condition     = startswith(var.iam_tenant_id, "tenant-")
    error_message = "ID of the IAM tenant must start with `tenant-`."
  }
}

data "nebius_iam_v1_tenant" "this" {
  id = var.iam_tenant_id
}

variable "o11y_iam_tenant_id" {
  description = "ID of the IAM tenant for O11y."
  type        = string
  nullable    = false

  validation {
    condition     = startswith(var.o11y_iam_tenant_id, "tenant-")
    error_message = "ID of the IAM tenant must start with `tenant-`."
  }
}

variable "o11y_profile" {
  description = "Profile for nebius CLI for public o11y."
  type        = string
  nullable    = false

  validation {
    condition = (
      (length(var.o11y_profile) >= 1 && var.public_o11y_enabled) ||
      !var.public_o11y_enabled
    )
    error_message = "O11y profile must be not empty if public o11y enabled is true."
  }
}

variable "production" {
  type    = bool
  default = true
}

variable "iam_merge_request_url" {
  type    = string
  default = ""

  validation {
    condition     = (var.production && length(var.iam_merge_request_url) > 0) || !var.production
    error_message = <<EOF
This variable must be set for PRODUCTION Soperator Pro clusters. Follow the installation guide and put IAM merge request URL here.

If you provision a NON-PRODUCTION cluster, set "production" variable to false.
    EOF
  }
}

variable "vpc_subnet_id" {
  description = "ID of VPC subnet."
  type        = string

  validation {
    condition     = startswith(var.vpc_subnet_id, "vpcsubnet-")
    error_message = "The ID of the VPC subnet must start with `vpcsubnet-`."
  }
}
data "nebius_vpc_v1_subnet" "this" {
  id = var.vpc_subnet_id
}

variable "slurm_login_public_ip" {
  description = "Public or private ip for login node load balancer"
  type        = bool
  default     = true
}

variable "tailscale_enabled" {
  description = "Whether to enable tailscale init container on login pod"
  type        = bool
  default     = false
}

variable "company_name" {
  description = "Name of the company. It is used for naming Slurm & K8s clusters."
  type        = string

  validation {
    condition = (
      length(var.company_name) >= 1 &&
      length(var.company_name) <= 32 &&
      length(regexall("^[a-z][a-z\\d\\-]*[a-z\\d]+$", var.company_name)) == 1
    )
    error_message = <<EOF
      The company name must:
      - be 1 to 32 characters long
      - start with a letter
      - end with a letter or digit
      - consist of letters, digits, or hyphens (-)
      - contain only lowercase letters
    EOF
  }
}

# endregion Cloud

# region Infrastructure

# region Storage

variable "controller_state_on_filestore" {
  description = "Whether to use Filestore for controller node boot disk (true = Filestore, false = PVC)."
  type        = bool
  default     = false
}

variable "filestore_controller_spool" {
  description = "Shared filesystem to be used on controller nodes."
  type = object({
    existing = optional(object({
      id = string
    }))
    spec = optional(object({
      size_gibibytes       = number
      block_size_kibibytes = number
      forbid_deletion      = optional(bool, false)
    }))
  })
  nullable = false

  validation {
    condition = (
      (var.filestore_controller_spool.existing != null && var.filestore_controller_spool.spec == null) ||
      (var.filestore_controller_spool.existing == null && var.filestore_controller_spool.spec != null)
    )
    error_message = "One of `existing` or `spec` must be provided."
  }
}

variable "filestore_jail" {
  description = "Shared filesystem to be used on controller, worker, and login nodes."
  type = object({
    existing = optional(object({
      id = string
    }))
    spec = optional(object({
      size_gibibytes       = number
      block_size_kibibytes = number
      forbid_deletion      = optional(bool, false)
    }))
  })
  nullable = false

  validation {
    condition = (
      (var.filestore_jail.existing != null && var.filestore_jail.spec == null) ||
      (var.filestore_jail.existing == null && var.filestore_jail.spec != null)
    )
    error_message = "One of `existing` or `spec` must be provided."
  }
}

data "nebius_compute_v1_filesystem" "existing_jail" {
  count = var.filestore_jail.existing != null ? 1 : 0

  id = var.filestore_jail.existing.id
}

locals {
  filestore_jail_calculated_size_gibibytes = (var.filestore_jail.existing != null ?
    data.nebius_compute_v1_filesystem.existing_jail[0].size_bytes / 1024 / 1024 / 1024 :
  var.filestore_jail.spec.size_gibibytes)
}

variable "allow_empty_jail_submounts" {
  description = "Flag for disabling validation for non-empty jail submounts."
  type        = bool
  default     = false
}

variable "filestore_jail_submounts" {
  description = "Shared filesystems to be mounted inside jail."
  type = list(object({
    name       = string
    mount_path = string
    existing = optional(object({
      id = string
    }))
    spec = optional(object({
      size_gibibytes       = number
      block_size_kibibytes = number
      forbid_deletion      = optional(bool, false)
    }))
  }))
  default = []

  validation {
    condition = length([
      for sm in var.filestore_jail_submounts : true if
      (sm.existing != null && sm.spec == null) ||
      (sm.existing == null && sm.spec != null)
    ]) == length(var.filestore_jail_submounts)
    error_message = "All submounts must have one of `existing` or `spec` provided."
  }

  validation {
    condition     = var.allow_empty_jail_submounts || length(var.filestore_jail_submounts) >= 1
    error_message = "Creating clusters without jail submounts is not allowed."
  }
}

variable "enroot_direct_squashfs_enabled" {
  description = "Enable Pyxis/Enroot direct SquashFS startup through squashfuse. Node-local image-storage disk creation remains controlled by node_local_image_disk.enabled."
  type        = bool
  default     = true
}

variable "filestore_accounting" {
  description = "Shared filesystem to be used for accounting DB"
  type = object({
    existing = optional(object({
      id = string
    }))
    spec = optional(object({
      size_gibibytes       = number
      block_size_kibibytes = number
      forbid_deletion      = optional(bool, false)
    }))
  })
  default  = null
  nullable = true

  validation {
    condition = (var.filestore_accounting != null
      ? (
        (var.filestore_accounting.existing != null && var.filestore_accounting.spec == null) ||
        (var.filestore_accounting.existing == null && var.filestore_accounting.spec != null)
      )
      : true
    )
    error_message = "One of `existing` or `spec` must be provided."
  }
}

# endregion Storage

# region nfs-server

variable "nfs" {
  type = object({
    enabled        = bool
    size_gibibytes = number
    mount_path     = optional(string, "/home")
    resource = object({
      platform = string
      preset   = string
    })
    public_ip = bool
  })
  default = {
    enabled        = false
    size_gibibytes = 93
    resource = {
      platform = "cpu-d3"
      preset   = "32vcpu-128gb"
    }
    public_ip = false
  }

  validation {
    condition = (var.nfs.enabled
      ? (
        var.nfs.size_gibibytes % 93 == 0 &&
        var.nfs.size_gibibytes <= 262074
      )
      : true
    )
    error_message = "NFS size must be a multiple of 93 GiB and maximum value is 262074 GiB"
  }
}
resource "terraform_data" "check_nfs_exclusivity" {
  lifecycle {
    precondition {
      condition     = !(var.nfs.enabled && var.nfs_in_k8s.enabled)
      error_message = "nfs.enabled and nfs_in_k8s.enabled cannot both be true. Choose one NFS backend: either an external NFS server (nfs.enabled) or the in-cluster NFS provisioner (nfs_in_k8s.enabled)."
    }
  }
}

resource "terraform_data" "check_jail_submount_paths" {
  lifecycle {
    precondition {
      condition = alltrue([
        for sm in var.filestore_jail_submounts :
        sm.mount_path != "/home"
      ])
      error_message = "filestore_jail_submounts must not use \"/home\" as mount_path. That path is reserved for home directories, and backing /home with shared filestore causes severe performance degradation."
    }
  }
}

resource "terraform_data" "check_nfs" {
  depends_on = [
    terraform_data.check_region,
  ]

  lifecycle {
    precondition {
      condition = (var.nfs.enabled
        ? contains(module.resources.platforms, var.nfs.resource.platform)
        : true
      )
      error_message = "Unsupported platform '${var.nfs.resource.platform}'."
    }

    precondition {
      condition = (var.nfs.enabled
        ? contains(keys(module.resources.by_platform[var.nfs.resource.platform]), var.nfs.resource.preset)
        : true
      )
      error_message = "Unsupported preset '${var.nfs.resource.preset}' for platform '${var.nfs.resource.platform}'."
    }

    precondition {
      condition = (var.nfs.enabled
        ? contains(module.resources.platform_regions[var.nfs.resource.platform], var.region)
        : true
      )
      error_message = "Unsupported platform '${var.nfs.resource.platform}' in region '${var.region}'. See https://docs.nebius.com/compute/virtual-machines/types"
    }
  }
}

variable "nfs_in_k8s" {
  type = object({
    enabled         = bool
    version         = optional(string)
    use_stable_repo = optional(bool, true)
    size_gibibytes  = optional(number)
    disk_type       = optional(string)
    filesystem_type = optional(string)
    threads         = optional(number)
  })
  default = {
    enabled = false
  }
  validation {
    condition = (
      !var.nfs_in_k8s.enabled
      ||
      (
        var.nfs_in_k8s.filesystem_type != null
        && var.nfs_in_k8s.disk_type != null
        && var.nfs_in_k8s.size_gibibytes != null
        && (
          !contains(["NETWORK_SSD_IO_M3", "NETWORK_SSD_NON_REPLICATED"], var.nfs_in_k8s.disk_type)
          || (var.nfs_in_k8s.size_gibibytes % 93 == 0)
        )
      )
    )

    error_message = <<EOT
If NFS in K8s is enabled, filesystem_type, disk_type, and size_gibibytes must be set.
Additionally, if disk_type is NETWORK_SSD_IO_M3 or NETWORK_SSD_NON_REPLICATED, size_gibibytes must be a multiple of 93.
EOT
  }

  validation {
    condition = (
      !var.nfs_in_k8s.enabled
      || var.nfs_in_k8s.disk_type == null
      || contains(["NETWORK_SSD", "NETWORK_SSD_NON_REPLICATED", "NETWORK_SSD_IO_M3"], var.nfs_in_k8s.disk_type)
    )
    error_message = "nfs_in_k8s.disk_type must be one of: NETWORK_SSD, NETWORK_SSD_NON_REPLICATED, NETWORK_SSD_IO_M3."
  }

  validation {
    condition = (
      !var.nfs_in_k8s.enabled
      || var.nfs_in_k8s.filesystem_type == null
      || contains(["ext4", "xfs"], var.nfs_in_k8s.filesystem_type)
    )
    error_message = "nfs_in_k8s.filesystem_type must be one of: ext4, xfs."
  }
}

# endregion nfs-server

# region k8s

variable "k8s_version" {
  description = "Version of the k8s to be used."
  type        = string
  default     = null

  validation {
    condition     = var.k8s_version == null || can(regex("^[\\d]+\\.[\\d]+$", var.k8s_version))
    error_message = "The k8s cluster version must be null or in format `<MAJOR>.<MINOR>`."
  }
}

variable "node_group_version" {
  description = "Version of the node group to be used. Contains bundle of different component versions, e.g. driver, linux_kernel, doca, etc."
  type        = string
}


variable "platform_cuda_versions" {
  description = "Per-platform CUDA versions consumed by Slurm/operator (e.g., 12.8.2). Keys are platform IDs (e.g., gpu-h100-sxm)."
  type        = map(string)
  default = {
    cpu-e1         = "12.9.0"
    cpu-e2         = "12.9.0"
    cpu-d3         = "12.9.0"
    gpu-l40s-a     = "13.0.2"
    gpu-l40s-d     = "13.0.2"
    gpu-h100-sxm   = "13.0.2"
    gpu-h200-sxm   = "13.0.2"
    gpu-b200-sxm   = "13.0.2"
    gpu-b200-sxm-a = "13.0.2"
    gpu-b300-sxm   = "13.0.2"
    gpu-rtx6000    = "13.0.2"
    gpu-gb300      = "13.0.2"
  }
}

variable "platform_driver_presets" {
  description = "Per-platform GPU driver presets. Keys are platform IDs (e.g., gpu-h100-sxm); values are driver presets (e.g., cuda13.0)."
  type        = map(string)
  default = {
    cpu-e1         = null
    cpu-e2         = null
    cpu-d3         = null
    gpu-l40s-a     = "cuda13.0"
    gpu-l40s-d     = "cuda13.0"
    gpu-h100-sxm   = "cuda13.0"
    gpu-h200-sxm   = "cuda13.0"
    gpu-b200-sxm   = "cuda13.0"
    gpu-b200-sxm-a = "cuda13.0"
    gpu-b300-sxm   = "cuda13.0"
    gpu-rtx6000    = "cuda13.0"
    gpu-gb300      = "cuda13.0"
  }
}

variable "use_preinstalled_gpu_drivers" {
  description = "Enable preinstalled mode for worker nodes."
  type        = bool
  default     = false
}

variable "nvidia_config_lines" {
  description = "Lines to write to /etc/modprobe.d/nvidia_config.conf via cloud-init (GPU workers only)."
  type        = list(string)
  default     = []
}

variable "k8s_cluster_node_ssh_access_users" {
  description = "SSH user credentials for accessing k8s nodes."
  type = list(object({
    name        = string
    public_keys = list(string)
  }))
  nullable = false
  default  = []

  validation {
    condition = alltrue([
      for u in var.k8s_cluster_node_ssh_access_users : length(u.public_keys) >= 1
    ])
    error_message = "Each entry in k8s_cluster_node_ssh_access_users must have at least one public key."
  }

  validation {
    condition = alltrue(flatten([
      for u in var.k8s_cluster_node_ssh_access_users : [
        for k in u.public_keys : length(k) > 0
      ]
    ]))
    error_message = "Public keys in k8s_cluster_node_ssh_access_users must not be empty strings."
  }
}

variable "k8s_cluster_node_ssh_access_public_ip" {
  description = "Assign public IP addresses to k8s nodes when k8s_cluster_node_ssh_access_users is configured."
  type        = bool
  nullable    = false
  default     = false
}

# endregion k8s

# endregion Infrastructure

# region Slurm

variable "slurm_operator_version" {
  description = "Version of soperator."
  type        = string
  nullable    = false
}

variable "slurm_operator_stable" {
  description = "Is the version of soperator stable."
  type        = bool
  default     = true
}

variable "slurm_nodesets_partitions" {
  description = <<-EOT
    Partition configuration for generated Slurm NodeSets.
    slurm_nodeset_refs must reference generated Slurm NodeSet names. A non-GB worker keeps its Terraform worker nodeset name.
    A GB300 worker nodeset expands into rack-scoped Slurm NodeSets named <name>-rack<rack>.
    Users must not remove the "hidden" partition.
    Users can modify the "main" partition, but should not remove it (there must be at least one default partition).
    topology is required. Available topologies: flat is always present, tree-ib is present for GPU NodeSets, and block-nvl72 is present for GB300 NodeSets.
  EOT
  type = list(object({
    name               = string
    is_all             = optional(bool, false)
    slurm_nodeset_refs = optional(list(string), [])
    topology           = string
    config             = string
  }))
  default = []

  validation {
    condition = alltrue([
      for p in var.slurm_nodesets_partitions :
      p.is_all || length(p.slurm_nodeset_refs) > 0
    ])
    error_message = "Each partition must have either is_all = true or non-empty slurm_nodeset_refs."
  }

  validation {
    condition = alltrue([
      for p in var.slurm_nodesets_partitions :
      !(p.is_all && length(p.slurm_nodeset_refs) > 0)
    ])
    error_message = "A partition cannot have both is_all = true and non-empty slurm_nodeset_refs."
  }

  validation {
    # Validate partition refs against generated Slurm NodeSet names, not raw
    # Terraform worker nodeset names. Example: gpu-gb300 worker "primtrain" with
    # size = 36 generates ["primtrain-rack0", "primtrain-rack1"];
    # non-GB worker "worker" stays "worker".
    condition = length(setsubtract(
      toset(flatten([
        for p in var.slurm_nodesets_partitions : coalesce(p.slurm_nodeset_refs, [])
      ])),
      toset(flatten([
        for w in var.slurm_nodeset_workers :
        w.resource.platform == "gpu-gb300" ? [
          for rack in range(max(1, try(ceil(w.size / 18), 0))) : format(
            "%s-rack%d",
            w.name,
            rack,
          )
        ] : [w.name]
      ]))
    )) == 0

    error_message = "All slurm_nodesets_partitions[].slurm_nodeset_refs must reference generated Slurm NodeSet names. GB300 worker nodesets generate <name>-rack<rack> names; other worker nodesets use <name>."
  }

  validation {
    condition = alltrue([
      for partition in var.slurm_nodesets_partitions :
      contains(
        concat(
          ["flat"],
          anytrue([for worker in var.slurm_nodeset_workers : startswith(worker.resource.platform, "gpu-")]) ? ["tree-ib"] : [],
          anytrue([for worker in var.slurm_nodeset_workers : worker.resource.platform == "gpu-gb300"]) ? ["block-nvl72"] : [],
        ),
        partition.topology,
      )
    ])
    error_message = "Each partition topology must be one of the topologies created for the configured NodeSets: flat; tree-ib for GPU NodeSets; block-nvl72 for GB300 NodeSets."
  }
}

# region PartitionConfiguration

variable "slurm_partition_config_type" {
  description = "Type of the Slurm partition config. Could be either `default` or `custom`."
  default     = "default"
  type        = string

  validation {
    condition     = (contains(["default", "custom"], var.slurm_partition_config_type))
    error_message = "Invalid partition config type. It must be one of `default` or `custom`."
  }
}

variable "slurm_partition_raw_config" {
  description = "Partition config in case of `custom` slurm_partition_config_type. Each string must be started with `PartitionName`."
  default     = []
  type        = list(string)
}

# endregion PartitionConfiguration

# region HealthCheckConfig

variable "slurm_health_check_config" {
  description = "Health check configuration."
  type = object({
    health_check_interval = number
    health_check_program  = string
    health_check_node_state = list(object({
      state = string
    }))
  })
  nullable = true
  default  = null
}

# endregion HealthCheckConfig

# region Nodes

variable "slurm_nodeset_system" {
  description = "Configuration of System node set for system resources created by Soperator."
  type = object({
    min_size = number
    max_size = number
    resource = object({
      platform = string
      preset   = optional(string)
    })
    boot_disk = object({
      type                 = string
      size_gibibytes       = number
      block_size_kibibytes = number
    })
  })
  nullable = false
  default = {
    min_size = 3
    max_size = 9
    resource = {
      platform = "cpu-d3"
      # preset defaults to the sizing tier's node preset (see local.slurm_nodeset_system).
    }
    boot_disk = {
      type                 = "NETWORK_SSD"
      size_gibibytes       = 128
      block_size_kibibytes = 4
    }
  }
  validation {
    condition     = var.slurm_nodeset_system.boot_disk.size_gibibytes >= 128
    error_message = "Boot disks for system nodes must be at least 128 GiB."
  }
  validation {
    condition     = var.slurm_nodeset_system.min_size >= 3
    error_message = "Minimum size of the system node group must be at least 3."
  }
  validation {
    condition     = var.slurm_nodeset_system.max_size >= var.slurm_nodeset_system.min_size
    error_message = "System nodeset max_size must be greater than or equal to min_size."
  }
}

variable "sizing_tier_override" {
  description = <<-EOT
    Force the sizing tier that dispatches system/observability component resources and CPU node presets by scale.
    One of XS, S, M, L, XL; null (default) auto-derives the tier from the total worker node count.
    Tier boundaries and per-tier values: soperator/modules/sizing_tier/main.tf.
  EOT
  type        = string
  default     = null

  validation {
    condition     = var.sizing_tier_override == null ? true : contains(["XS", "S", "M", "L", "XL"], var.sizing_tier_override)
    error_message = "sizing_tier_override must be one of: XS, S, M, L, XL."
  }
}

variable "component_overrides" {
  description = <<-EOT
    Optional per-component resource overrides applied on top of the sizing tier
    (an entry replaces that component's tier value wholesale; unset components
    keep their tier values). Same shape as the component_presets table in
    soperator/modules/sizing_tier/main.tf. Leave empty to let
    the sizing tier drive everything.
  EOT
  type = object({
    exporter                    = optional(object({ cpu = number, memory = number, ephemeral_storage = number }))
    rest                        = optional(object({ cpu = number, memory = number, ephemeral_storage = number }))
    mariadb                     = optional(object({ cpu = number, memory = number, ephemeral_storage = number }))
    node_configurator           = optional(object({ requests = object({ cpu = number, memory = number }), limits = object({ memory = number }) }))
    soperator_main_controller   = optional(object({ requests = object({ cpu = number, memory = number }), limits = object({ memory = number }) }))
    soperator_checks_controller = optional(object({ requests = object({ cpu = number, memory = number }), limits = object({ memory = number }) }))
    dcgm_exporter               = optional(object({ cpu = number, memory = number }))
    kruise_daemon               = optional(object({ cpu = number, memory = number }))
    nfs_server                  = optional(object({ cpu = number, memory = number }))
    spo_controller              = optional(object({ cpu = string, memory = string }))
    spo_daemon                  = optional(object({ cpu = string, memory = string }))
    kruise_manager              = optional(object({ cpu = string, memory = string }))
    kube_state_metrics          = optional(object({ requests = object({ cpu = string, memory = string }), limits = object({ memory = string }) }))
    vm_single                   = optional(object({ memory = string, cpu = string, size = string, gomaxprocs = number }))
    vm_agent                    = optional(object({ memory = string, cpu = string }))
    vm_logs                     = optional(object({ memory = string, cpu = string, size = string }))
    events_collector            = optional(object({ memory = string, cpu = string }))
    logs_collector              = optional(object({ memory = string, cpu = string }))
    jail_logs_collector         = optional(object({ memory = string, cpu = string }))
    nccl_profiles_collector     = optional(object({ memory = string, cpu = string }))
  })
  default  = {}
  nullable = false
}

variable "slurm_nodeset_controller" {
  description = "Configuration of Slurm Controller node set. Only a single controller node is supported."
  type = object({
    size = number
    resource = object({
      platform = string
      preset   = optional(string)
    })
    boot_disk = object({
      type                 = string
      size_gibibytes       = number
      block_size_kibibytes = number
    })
  })
  nullable = false
  default = {
    size = 1
    resource = {
      platform = "cpu-d3"
      # preset defaults to the sizing tier's node preset (see local.slurm_nodeset_controller).
    }
    boot_disk = {
      type                 = "NETWORK_SSD"
      size_gibibytes       = 128
      block_size_kibibytes = 4
    }
  }
  validation {
    condition     = var.slurm_nodeset_controller.boot_disk.size_gibibytes >= 128
    error_message = "Boot disks for controller nodes must be at least 128 GiB."
  }
  validation {
    condition     = var.slurm_nodeset_controller.size == 1
    error_message = "Size of the controller node group must be exactly 1."
  }
}

variable "slurm_nodeset_workers" {
  description = "Configuration of Slurm Worker node sets."
  type = list(object({
    name = string
    size = number
    autoscaling = optional(object({
      enabled  = optional(bool, true)
      min_size = optional(number, 0)
    }), {})
    resource = object({
      platform = string
      preset   = string
    })
    boot_disk = object({
      type                 = string
      size_gibibytes       = number
      block_size_kibibytes = number
    })
    gpu_cluster = optional(object({
      id                = optional(string)
      infiniband_fabric = optional(string)
    }))
    preemptible = optional(object({}))
    reservation_policy = optional(object({
      policy          = optional(string)
      reservation_ids = optional(list(string))
    }))
    nvlink = optional(object({
      enabled = optional(bool, false)
      type    = optional(string, "GB300")
    }), {})
    # Additional labels applied to every mk8s node in this worker nodeset.
    extra_labels                   = optional(map(string), {})
    placement_policy_nodes         = optional(list(string))
    features                       = optional(list(string))
    create_partition               = optional(bool)
    ephemeral_nodes                = optional(bool, false)
    initial_number_ephemeral_nodes = optional(number, 0)
    persistent_volume_claim_retention_policy = optional(object({
      when_deleted = string
      when_scaled  = string
    }))
    local_nvme = optional(object({
      enabled                   = optional(bool)
      device_count              = optional(number)
      device_capacity_gigabytes = optional(number)
      mount_path                = optional(string, "/mnt/local-nvme")
      size_limit_gibibytes      = optional(number)
    }), {})
    max_pods = optional(number, 32)
    node_local_image_disk = object({
      enabled = bool
      spec = optional(object({
        size_gibibytes  = number
        filesystem_type = string
        disk_type       = string
      }))
    })
    node_local_jail_submounts = list(object({
      name            = string
      mount_path      = string
      size_gibibytes  = number
      disk_type       = string
      filesystem_type = string
    }))
  }))
  nullable = false
  default = [{
    name = "worker"
    size = 1
    resource = {
      platform = "cpu-d3"
      preset   = "16vcpu-64gb"
    }
    boot_disk = {
      type                 = "NETWORK_SSD"
      size_gibibytes       = 128
      block_size_kibibytes = 4
    }
    node_local_image_disk = {
      enabled = false
    }
    node_local_jail_submounts = []
  }]

  validation {
    condition = alltrue([
      for worker in var.slurm_nodeset_workers :
      worker.gpu_cluster == null || (
        length(try(trimspace(worker.gpu_cluster.id), "")) > 0 ||
        length(try(trimspace(worker.gpu_cluster.infiniband_fabric), "")) > 0
      )
    ])
    error_message = "slurm_nodeset_workers.gpu_cluster must set either id or infiniband_fabric."
  }

  validation {
    condition = alltrue([
      for worker in var.slurm_nodeset_workers :
      worker.resource.platform == "gpu-gb300" ? (
        var.production
        ? try(worker.size % local.gb300_nodes_per_nodegroup == 0, false)
        : try(worker.size < local.gb300_nodes_per_nodegroup || worker.size % local.gb300_nodes_per_nodegroup == 0, false)
      ) : true
    ])
    error_message = "GB300 worker nodesets must have size divisible by ${local.gb300_nodes_per_nodegroup}."
  }

  validation {
    # NVLink is modeled only for GB300 here: GB300 must enable it and all other
    # platforms must leave it disabled.
    condition = alltrue([
      for worker in var.slurm_nodeset_workers :
      worker.resource.platform == "gpu-gb300" ? try(worker.nvlink.enabled == true, false) : !try(worker.nvlink.enabled == true, false)
    ])
    error_message = "NVLink must be enabled for gpu-gb300 worker nodesets and disabled for all other platforms."
  }

  validation {
    # The provider requires a type value for NVLink instance groups. This
    # installation path supports only GB300 groups.
    condition = alltrue([
      for worker in var.slurm_nodeset_workers :
      worker.resource.platform == "gpu-gb300" ? try(coalesce(worker.nvlink.type, "GB300") == "GB300", false) : true
    ])
    error_message = "GB300 worker nodesets must use nvlink.type = \"GB300\"."
  }

  validation {
    # placement_policy_nodes is a per-worker list. In production it must be
    # absent or empty; non-production may pin node groups to provider nodes.
    condition = !var.production || alltrue([
      for worker in var.slurm_nodeset_workers :
      length(coalesce(worker.placement_policy_nodes, [])) == 0
    ])
    error_message = "Worker placement_policy_nodes can only be set when production = false."
  }

  validation {
    condition     = length(var.slurm_nodeset_workers) > 0
    error_message = "At least one worker nodeset must be provided."
  }

  validation {
    # Compare the set of generated worker NodeSet names with the full generated
    # name list; a shorter distinct list means two inputs collide after
    # expansion. Example collision: two non-GB workers named "worker" both
    # generate "worker".
    condition = length(distinct(flatten([
      for worker in var.slurm_nodeset_workers :
      worker.resource.platform == "gpu-gb300" ? [
        for rack in range(max(1, try(ceil(worker.size / 18), 0))) : format(
          "%s-rack%d",
          worker.name,
          rack,
        )
      ] : [worker.name]
      ]))) == length(flatten([
      for worker in var.slurm_nodeset_workers :
      worker.resource.platform == "gpu-gb300" ? [
        for rack in range(max(1, try(ceil(worker.size / 18), 0))) : format(
          "%s-rack%d",
          worker.name,
          rack,
        )
      ] : [worker.name]
    ]))
    error_message = "All effective worker nodeset names must be unique. GB300 worker nodesets are named <name>-rack<rack>; other worker nodesets use <name>."
  }

  validation {
    condition = alltrue([
      for worker in var.slurm_nodeset_workers :
      (worker.boot_disk.size_gibibytes >= 128)
    ])
    error_message = "Boot disks for worker nodes must be at least 128 GiB."
  }

  validation {
    condition = alltrue([
      for worker in var.slurm_nodeset_workers :
      worker.autoscaling.min_size == null || worker.autoscaling.min_size <= worker.size
    ])
    error_message = "Worker nodeset autoscaling.min_size must be less than or equal to size."
  }

  validation {
    condition = alltrue([
      for worker in var.slurm_nodeset_workers :
      worker.max_pods > 0
    ])
    error_message = "Worker nodeset max_pods must be greater than 0."
  }

  validation {
    condition = alltrue([
      for worker in var.slurm_nodeset_workers :
      !coalesce(worker.local_nvme.enabled, contains(local.local_nvme_default_enabled_platforms, worker.resource.platform)) || (
        startswith(try(worker.local_nvme.mount_path, "/mnt/local-nvme"), "/")
      )
    ])
    error_message = "When worker local NVMe is enabled, mount_path must be an absolute path."
  }

  validation {
    condition = alltrue([
      for worker in var.slurm_nodeset_workers :
      !coalesce(worker.local_nvme.enabled, contains(local.local_nvme_default_enabled_platforms, worker.resource.platform)) || (
        try(worker.local_nvme.device_count > 0, false) &&
        try(worker.local_nvme.device_count == floor(worker.local_nvme.device_count), false) &&
        try(worker.local_nvme.device_capacity_gigabytes > 0, false)
      )
    ])
    error_message = "When worker local NVMe is enabled, device_count must be a positive integer and device_capacity_gigabytes must be greater than 0."
  }

  validation {
    condition = alltrue([
      for worker in var.slurm_nodeset_workers :
      try(worker.local_nvme.size_limit_gibibytes > 0, true)
    ])
    error_message = "When worker local NVMe size_limit_gibibytes is set, it must be greater than 0."
  }

  validation {
    condition = alltrue([
      for worker in var.slurm_nodeset_workers :
      worker.node_local_image_disk.enabled ?
      worker.node_local_image_disk.spec != null : true
    ])
    error_message = "slurm_nodeset_workers.node_local_image_disk.spec must be provided if enabled."
  }
  validation {
    condition = alltrue([
      for worker in var.slurm_nodeset_workers :
      worker.node_local_image_disk.spec == null
      ? true
      : (contains(
        [
          module.resources.filesystem_types.ext4,
          module.resources.filesystem_types.xfs,
        ],
        worker.node_local_image_disk.spec.filesystem_type
      ))
    ])
    error_message = "slurm_nodeset_workers.node_local_image_disk.spec.filesystem_type must be one of `ext4` or `xfs`."
  }
  validation {
    condition = alltrue([
      for worker in var.slurm_nodeset_workers :
      worker.node_local_image_disk.spec == null
      ? true
      : (contains(
        [
          module.resources.disk_types.network_ssd_non_replicated,
          module.resources.disk_types.network_ssd_io_m3,
        ],
        worker.node_local_image_disk.spec.disk_type
      ))
    ])
    error_message = "Local image disk type must be one of `NETWORK_SSD_NON_REPLICATED` or `NETWORK_SSD_IO_M3`. See https://docs.nebius.com/compute/storage/types#disks-types"
  }
  validation {
    condition = alltrue(flatten([
      for worker in var.slurm_nodeset_workers : [
        for sm in worker.node_local_jail_submounts : (
          contains(
            [
              module.resources.disk_types.network_ssd,
              module.resources.disk_types.network_ssd_non_replicated,
              module.resources.disk_types.network_ssd_io_m3,
            ],
            sm.disk_type
          )
        )
      ]
    ]))
    error_message = "Disk type must be one of `NETWORK_SSD`, `NETWORK_SSD_NON_REPLICATED` or `NETWORK_SSD_IO_M3`. See https://docs.nebius.com/compute/storage/types#disks-types"
  }
  validation {
    condition = alltrue(flatten([
      for worker in var.slurm_nodeset_workers : [
        for sm in worker.node_local_jail_submounts : (
          contains(
            [
              module.resources.filesystem_types.ext4,
              module.resources.filesystem_types.xfs,
            ],
            sm.filesystem_type
          )
        )
      ]
    ]))
    error_message = "Filesystem type must be one of `ext4` or `xfs`."
  }

  validation {
    condition = alltrue([
      for worker in var.slurm_nodeset_workers :
      worker.persistent_volume_claim_retention_policy == null || (
        contains(["Retain", "Delete"], worker.persistent_volume_claim_retention_policy.when_deleted) &&
        contains(["Retain", "Delete"], worker.persistent_volume_claim_retention_policy.when_scaled)
      )
    ])
    error_message = "When worker persistent_volume_claim_retention_policy is set, when_deleted and when_scaled must be `Retain` or `Delete`."
  }
}

variable "slurm_nodeset_login" {
  description = "Configuration of Slurm Login node set. Login pod autoscaling is disabled by default. When enabled, its replica bounds override size for login pods."
  type = object({
    size               = number
    node_group_enabled = optional(bool, true)
    autoscaling = optional(object({
      enabled                           = bool
      min_size                          = optional(number, 1)
      max_size                          = optional(number, 4)
      target_cpu_utilization_percentage = optional(number, 70)
    }))
    resource = object({
      platform = string
      preset   = string
    })
    boot_disk = object({
      type                 = string
      size_gibibytes       = number
      block_size_kibibytes = number
    })
  })
  nullable = false
  default = {
    size = 1
    resource = {
      platform = "cpu-d3"
      preset   = "16vcpu-64gb"
    }
    boot_disk = {
      type                 = "NETWORK_SSD"
      size_gibibytes       = 256
      block_size_kibibytes = 4
    }
  }
  validation {
    condition     = var.slurm_nodeset_login.boot_disk.size_gibibytes >= 256
    error_message = "Boot disks for login nodes must be at least 256 GiB."
  }
  validation {
    condition     = var.slurm_nodeset_login.size >= 1
    error_message = "Login replica count (slurm_nodeset_login.size) must be at least 1."
  }
  validation {
    condition = var.slurm_nodeset_login.autoscaling == null ? true : (
      var.slurm_nodeset_login.autoscaling.min_size >= 1 &&
      floor(var.slurm_nodeset_login.autoscaling.min_size) == var.slurm_nodeset_login.autoscaling.min_size &&
      var.slurm_nodeset_login.autoscaling.max_size >= var.slurm_nodeset_login.autoscaling.min_size &&
      floor(var.slurm_nodeset_login.autoscaling.max_size) == var.slurm_nodeset_login.autoscaling.max_size
    )
    error_message = "Login autoscaling min_size and max_size must be whole numbers, min_size must be at least 1, and max_size must be greater than or equal to min_size."
  }
  validation {
    condition = var.slurm_nodeset_login.autoscaling == null ? true : (
      var.slurm_nodeset_login.autoscaling.target_cpu_utilization_percentage >= 1 &&
      var.slurm_nodeset_login.autoscaling.target_cpu_utilization_percentage <= 100 &&
      floor(var.slurm_nodeset_login.autoscaling.target_cpu_utilization_percentage) == var.slurm_nodeset_login.autoscaling.target_cpu_utilization_percentage
    )
    error_message = "Login autoscaling target_cpu_utilization_percentage must be a whole number from 1 through 100."
  }
}

variable "gb300_login_pod_worker_reserve" {
  description = "Resources requested by each GB300 login pod and reserved from GB300 worker node capacity when login pods run on worker nodes instead of a dedicated CPU login node group."
  type = object({
    cpu_cores                   = number
    memory_gibibytes            = number
    ephemeral_storage_gibibytes = number
  })
  nullable = false
  default = {
    cpu_cores                   = 8
    memory_gibibytes            = 32
    ephemeral_storage_gibibytes = 128
  }

  validation {
    condition = (
      !anytrue([for worker in var.slurm_nodeset_workers : worker.resource.platform == "gpu-gb300"]) ||
      (
        var.gb300_login_pod_worker_reserve.cpu_cores > 0 &&
        var.gb300_login_pod_worker_reserve.memory_gibibytes > 0 &&
        var.gb300_login_pod_worker_reserve.ephemeral_storage_gibibytes > 0
      )
    )
    error_message = "GB300 login pod worker reserve values must be greater than zero when GB300 workers are configured."
  }
}

variable "slurm_nodeset_accounting" {
  description = "Configuration of Slurm Accounting node set."
  type = object({
    resource = object({
      platform = string
      preset   = optional(string)
    })
    boot_disk = object({
      type                 = string
      size_gibibytes       = number
      block_size_kibibytes = number
    })
  })
  default = {
    resource = {
      platform = "cpu-d3"
      # preset defaults to the sizing tier's node preset (see local.slurm_nodeset_accounting).
    }
    boot_disk = {
      type                 = "NETWORK_SSD"
      size_gibibytes       = 128
      block_size_kibibytes = 4
    }
  }
  validation {
    condition     = var.slurm_nodeset_accounting.boot_disk.size_gibibytes >= 128
    error_message = "Boot disks for accounting nodes must be at least 128 GiB."
  }
}

resource "terraform_data" "check_slurm_nodeset_accounting" {
  lifecycle {
    precondition {
      condition = (var.accounting_enabled
        ? var.slurm_nodeset_accounting != null
        : true
      )
      error_message = "Accounting node set must be provided when accounting is enabled."
    }
  }
}

variable "slurm_nodeset_nfs" {
  description = "Configuration of NFS node set."
  type = object({
    size = number
    resource = object({
      platform = string
      preset   = optional(string)
    })
    boot_disk = object({
      type                 = string
      size_gibibytes       = number
      block_size_kibibytes = number
    })
  })
  nullable = true
  default  = null
  validation {
    condition     = var.slurm_nodeset_nfs == null || var.slurm_nodeset_nfs.boot_disk.size_gibibytes >= 128
    error_message = "Boot disks for NFS nodes must be at least 128 GiB."
  }
  validation {
    condition     = var.slurm_nodeset_nfs == null || var.slurm_nodeset_nfs.size == 1
    error_message = "Size of the NFS node group must be exactly 1."
  }
}

resource "terraform_data" "check_slurm_nodeset" {
  for_each = merge({
    "system"     = local.slurm_nodeset_system
    "controller" = local.slurm_nodeset_controller
    "login"      = var.slurm_nodeset_login
    }, { for i, worker in var.slurm_nodeset_workers :
    "worker_${i}" => worker
    },
    local.slurm_nodeset_nfs != null ? {
      "nfs" = local.slurm_nodeset_nfs
    } : {}
  )

  depends_on = [
    terraform_data.check_region,
  ]

  lifecycle {
    precondition {
      condition = (
        startswith(each.key, "worker_")
        ? (
          try(each.value.size >= 0 && floor(each.value.size) == each.value.size, false) &&
          (
            try(each.value.autoscaling.min_size, null) == null
            ? true
            : try(each.value.autoscaling.min_size >= 0 && floor(each.value.autoscaling.min_size) == each.value.autoscaling.min_size, false)
          )
        )
        : (
          try(each.value.size > 0 && floor(each.value.size) == each.value.size, false) ||
          try(each.value.min_size > 0 && floor(each.value.min_size) == each.value.min_size, false)
        )
      )
      error_message = "Node set ${each.key} must have whole-number size/min_size values. Worker node sets may use size = 0 and validate autoscaling.min_size when set; other node sets must have size or min_size greater than 0."
    }

    precondition {
      condition     = contains(module.resources.platforms, each.value.resource.platform)
      error_message = "Unsupported platform '${each.value.resource.platform}' in node set '${each.key}'."
    }

    precondition {
      condition     = contains(keys(module.resources.by_platform[each.value.resource.platform]), each.value.resource.preset)
      error_message = "Unsupported preset '${each.value.resource.preset}' for platform '${each.value.resource.platform}' in node set '${each.key}'."
    }

    precondition {
      condition     = contains(module.resources.platform_regions[each.value.resource.platform], var.region)
      error_message = "Unsupported platform '${each.value.resource.platform}' in region '${var.region}'. See https://docs.nebius.com/compute/virtual-machines/types"
    }

    # TODO: precondition for total node group count
  }
}

locals {
  slurm_worker_cpu_platform_entries = [
    for worker in var.slurm_nodeset_workers : {
      name         = worker.name
      platform     = worker.resource.platform
      cpu_platform = try(module.resources.by_platform[worker.resource.platform][worker.resource.preset].cpu_platform, "")
    }
  ]

  slurm_worker_cpu_platforms = compact([
    for entry in local.slurm_worker_cpu_platform_entries : entry.cpu_platform
  ])

  slurm_worker_cpu_platform_message = join("\n", [
    for entry in local.slurm_worker_cpu_platform_entries :
    format("%s (%s) -> %s", entry.name, entry.platform, entry.cpu_platform)
  ])
}

resource "terraform_data" "check_slurm_worker_cpu_platform" {
  depends_on = [
    terraform_data.check_slurm_nodeset,
  ]

  lifecycle {
    precondition {
      # Worker nodesets share binaries through one jail filesystem, so all
      # worker nodesets must be binary-compatible.
      condition     = length(distinct(local.slurm_worker_cpu_platforms)) <= 1
      error_message = "Slurm worker nodesets must use the same CPU platform because they share one jail filesystem.\nConfigured CPU platforms:\n${local.slurm_worker_cpu_platform_message}"
    }
  }
}

resource "terraform_data" "check_local_nvme" {
  lifecycle {
    precondition {
      condition = (
        !anytrue([
          for worker in local.slurm_nodeset_workers_with_defaults :
          worker.local_nvme.enabled
        ]) ||
        alltrue([
          for worker in local.slurm_nodeset_workers_with_defaults :
          !worker.local_nvme.enabled || (
            try(module.resources.local_nvme_supported_by_region_platform_preset[var.region][worker.resource.platform][worker.resource.preset], false)
          )
        ])
      )
      error_message = "Local NVMe is enabled, but one or more worker nodesets use unsupported region/platform/preset."
    }

    precondition {
      condition = alltrue([
        for i, worker in local.slurm_nodeset_workers :
        !worker.local_nvme.enabled || try(
          worker.local_nvme.size_limit_gibibytes <= local.worker_ephemeral_storage_capacity_gibibytes[i],
          true,
        )
      ])
      error_message = "Local NVMe size_limit_gibibytes cannot exceed the usable ephemeral-storage capacity calculated from the configured devices."
    }
  }
}

# region Worker

variable "slurm_worker_sshd_config_map_ref_name" {
  description = "Name of configmap with SSHD config, which runs in slurmd container."
  type        = string
  default     = ""
}

# endregion Worker

# region Login

variable "slurm_login_sshd_config_map_ref_name" {
  description = "Name of configmap with SSHD config, which runs in slurmd container."
  type        = string
  default     = ""
}

variable "slurm_sssd_conf_secret_ref_name" {
  description = "Name of Secret containing sssd.conf propagated to controller, login, and worker sssd containers."
  type        = string
  default     = ""
}

variable "slurm_sssd_ldap_ca_config_map_ref_name" {
  description = "Name of ConfigMap containing LDAP CA certificates propagated to controller, login, and worker sssd containers."
  type        = string
  default     = ""
}

variable "slurm_sssd_enabled" {
  description = "Whether to enable the SSSD sidecar on Slurm controller, login, and worker nodes."
  type        = bool
  default     = false
}

variable "slurm_login_ssh_root_public_keys" {
  description = "Authorized keys accepted for connecting to Slurm login nodes via SSH as 'root' user."
  type        = list(string)
  nullable    = false

  validation {
    condition     = length(var.slurm_login_ssh_root_public_keys) >= 1
    error_message = "At least one SSH public key must be provided."
  }

  validation {
    condition     = alltrue([for k in var.slurm_login_ssh_root_public_keys : length(k) > 0])
    error_message = "SSH public keys must not be empty strings."
  }
}

# endregion Login

# region Exporter

variable "slurm_exporter_enabled" {
  description = "Whether to enable Slurm metrics exporter."
  type        = bool
  default     = true
}

variable "slurm_exporter_max_collector_inflight" {
  description = "Maximum number of concurrent collections per collector in Slurm exporter."
  type        = number
  default     = 1

  validation {
    condition     = var.slurm_exporter_max_collector_inflight >= 1
    error_message = "slurm_exporter_max_collector_inflight must be greater than or equal to 1."
  }
}

# endregion Exporter

# region REST API

variable "slurm_rest_enabled" {
  description = "Whether to enable Slurm REST API."
  type        = bool
  default     = true
}

# endregion REST API

# endregion Nodes

# region Config

variable "slurm_shared_memory_size_gibibytes" {
  description = "Shared memory size for Slurm controller and worker nodes in GiB."
  type        = number
  default     = 64

  validation {
    condition     = var.slurm_shared_memory_size_gibibytes > 0
    error_message = "slurm_shared_memory_size_gibibytes must be greater than 0."
  }
}

variable "slurm_topology_block_size" {
  description = <<EOL
    Block size for Slurm topology/block topology plugin in number of nodes.
    This affects how Slurm groups nodes into blocks for scheduling purposes.
    A smaller block size allows for more flexible scheduling but may increase overhead,
    while a larger block size may improve scheduling efficiency but reduce flexibility.
    The optimal value depends on the cluster size and workload characteristics.
  EOL
  type        = number
  default     = 18
  nullable    = true

  validation {
    condition     = try(var.slurm_topology_block_size > 0, true)
    error_message = "slurm_topology_block_size must be greater than 0 if set."
  }
}

# endregion Config

# region Telemetry

variable "telemetry_enabled" {
  description = "Whether to enable telemetry."
  type        = bool
  default     = true
}

variable "public_o11y_enabled" {
  description = "Whether to enable public observability endpoints."
  type        = bool
  default     = true
}

variable "allow_o11y_region_migration" {
  description = "Whether to update an existing o11y logs project when its region differs from var.region."
  type        = bool
  default     = false
}

variable "dcgm_exporter_enabled" {
  description = "Whether to install soperator's dcgm-exporter chart. When false, the NVIDIA gpu-operator's stock dcgm-exporter is used instead."
  type        = bool
  default     = true
}

variable "kube_state_metrics_max_scrape_size" {
  description = "Maximum kube-state-metrics HTTP scrape size in bytes. Leave null to let the sizing tier decide (raised automatically on M and larger clusters)."
  type        = number
  default     = null
  nullable    = true

  validation {
    condition     = var.kube_state_metrics_max_scrape_size == null || var.kube_state_metrics_max_scrape_size > 0
    error_message = "kube_state_metrics_max_scrape_size must be greater than 0 when set."
  }
}

variable "opentelemetry_batch" {
  description = "OpenTelemetry sending_queue batch overrides for the in-cluster (VictoriaLogs/VictoriaMetrics) exporters of the logs, jail logs, events, and nccl-profiles collectors. Does not affect the public Cloud Logging exporter, whose batching is managed by the chart (publicBatch, capped at 1000 records per request). Leave null to use chart defaults."
  type = object({
    timeout             = optional(string)
    send_batch_size     = optional(number)
    send_batch_max_size = optional(number)
  })
  default  = null
  nullable = true

  validation {
    condition = (
      var.opentelemetry_batch == null ||
      var.opentelemetry_batch.timeout == null ||
      trimspace(var.opentelemetry_batch.timeout) != ""
    )
    error_message = "opentelemetry_batch.timeout must be non-empty when set."
  }

  validation {
    condition = (
      var.opentelemetry_batch == null ||
      var.opentelemetry_batch.send_batch_size == null ||
      var.opentelemetry_batch.send_batch_size > 0
    )
    error_message = "opentelemetry_batch.send_batch_size must be greater than 0 when set."
  }

  validation {
    condition = (
      var.opentelemetry_batch == null ||
      var.opentelemetry_batch.send_batch_max_size == null ||
      var.opentelemetry_batch.send_batch_max_size > 0
    )
    error_message = "opentelemetry_batch.send_batch_max_size must be greater than 0 when set."
  }

  validation {
    condition = (
      var.opentelemetry_batch == null ||
      var.opentelemetry_batch.send_batch_size == null ||
      var.opentelemetry_batch.send_batch_max_size == null ||
      var.opentelemetry_batch.send_batch_max_size >= var.opentelemetry_batch.send_batch_size
    )
    error_message = "opentelemetry_batch.send_batch_max_size must be greater than or equal to send_batch_size when both are set."
  }
}

variable "opentelemetry_sending_queue" {
  description = "OpenTelemetry sending_queue overrides for logs, jail logs, events, and nccl-profiles collectors. Leave null to use chart defaults."
  type = object({
    size          = optional(number)
    num_consumers = optional(number)
  })
  default  = null
  nullable = true

  validation {
    condition = (
      var.opentelemetry_sending_queue == null ||
      var.opentelemetry_sending_queue.size == null ||
      var.opentelemetry_sending_queue.size > 0
    )
    error_message = "opentelemetry_sending_queue.size must be greater than 0 when set."
  }

  validation {
    condition = (
      var.opentelemetry_sending_queue == null ||
      var.opentelemetry_sending_queue.num_consumers == null ||
      var.opentelemetry_sending_queue.num_consumers > 0
    )
    error_message = "opentelemetry_sending_queue.num_consumers must be greater than 0 when set."
  }
}

variable "opentelemetry_delete_jail_logs_after_read" {
  description = "Whether to delete jail stored logs after they have been read by the OpenTelemetry collector."
  type        = bool
  default     = true
}

variable "opentelemetry_delete_jail_logs_min_age" {
  description = "Minimum time a jail log file must remain unmodified before the OpenTelemetry collector deletes it after reading. Logs are node-local (worker boot disk), so this is also the on-node debugging window."
  type        = string
  default     = "4h"

  validation {
    condition     = can(regex("^[0-9]+(s|m|h)$", var.opentelemetry_delete_jail_logs_min_age))
    error_message = "Must be a Go-style duration with a single unit, e.g. 90s, 30m, or 4h."
  }
}

variable "soperator_notifier" {
  description = "Configuration of the Soperator Notifier (https://github.com/nebius/soperator/tree/main/helm/soperator-notifier)."
  type = object({
    enabled           = bool
    slack_webhook_url = optional(string)
  })
  default = {
    enabled = false
  }
  nullable = false

  validation {
    condition = (
      var.soperator_notifier.enabled
      ? coalesce(var.soperator_notifier.slack_webhook_url, "not_provided") != "not_provided"
      : true
    )
    error_message = "Slack webhook URL must be provided if Soperator Notifier is enabled."
  }
}

variable "nccl_inspector_profiling" {
  description = "Configuration of the NCCL Inspector profiling."
  type = object({
    enabled  = bool
    dump_dir = optional(string)
    verbose  = optional(bool)
  })
  default = {
    enabled = false
  }
  nullable = false
}

# endregion Telemetry

# region Accounting

variable "accounting_enabled" {
  description = "Whether to enable accounting."
  type        = bool
  default     = false
}

variable "slurmdbd_config" {
  description = "Slurmdbd.conf configuration. See https://slurm.schedmd.com/slurmdbd.conf.html.Not all options are supported."
  type        = map(any)
  default = {
    # archiveEvents : "yes"
    # archiveJobs : "yes"
    # archiveSteps : "yes"
    # archiveSuspend : "yes"
    # archiveResv : "yes"
    # archiveUsage : "yes"
    # archiveTXN : "yes"
    # debugLevel : "info"
    # tcpTimeout : 120
    # purgeEventAfter : "1month"
    # purgeJobAfter : "1month"
    # purgeStepAfter : "1month"
    # purgeSuspendAfter : "12month"
    # purgeResvAfter : "1month"
    # purgeUsageAfter : "1month"
    # debugFlags : "DB_ARCHIVE"
  }
}

variable "slurm_accounting_config" {
  description = "Slurm accounting settings rendered into Soperator-generated slurm_base.conf.noedit, which is included by slurm.conf. See upstream Slurm slurm.conf documentation: https://slurm.schedmd.com/slurm.conf.html. Not all options are supported."
  type        = map(any)
  default = {
    # accountingStorageTRES: "gres/gpu,license/iop1"
    # accountingStoreFlags: "job_comment,job_env,job_extra,job_script,no_stdio"
    # acctGatherInterconnectType: "acct_gather_interconnect/ofed"
    # acctGatherFilesystemType: "acct_gather_filesystem/lustre"
    # jobAcctGatherType: "jobacct_gather/cgroup"
    # jobAcctGatherFrequency: 30
    # priorityWeightAge: 1
    # priorityWeightFairshare: 1
    # priorityWeightQOS: 1
    # priorityWeightTRES: 1
  }
}

# endregion Accounting

# region Backups

variable "backups_enabled" {
  description = "Whether to enable jail backups. Choose from 'auto', 'force_enable' and 'force_disable'. 'auto' enables backups for jails with max size < 12 TB."
  type        = string
  default     = "auto"

  validation {
    condition     = contains(["auto", "force_enable", "force_disable"], var.backups_enabled)
    error_message = "Valid values for backups_enabled are 'auto', 'force_enable' and 'force_disable'"
  }
}

variable "backups_password" {
  description = "Password for encrypting jail backups."
  type        = string
  nullable    = false
}

variable "backups_schedule" {
  description = "Cron schedule for backup task."
  type        = string
  nullable    = false
}

variable "backups_prune_schedule" {
  description = "Cron schedule for prune task."
  type        = string
  nullable    = false
}

variable "backups_retention" {
  description = "Backups retention policy."
  type        = map(any)
}

variable "cleanup_bucket_on_destroy" {
  description = "Whether to delete on destroy all backup data from bucket or not"
  type        = bool
}

# endregion Backups

# region Apparmor
variable "use_default_apparmor_profile" {
  description = "Whether to use default AppArmor profile."
  type        = bool
  default     = true
}

# endregion Apparmor

# region Maintenance
variable "maintenance" {
  description = "Whether to enable maintenance mode."
  type        = string
  default     = "none"

  validation {
    condition     = contains(["downscaleAndDeletePopulateJail", "downscaleAndOverwritePopulateJail", "downscale", "none", "skipPopulateJail"], var.maintenance)
    error_message = "The maintenance variable must be one of: downscaleAndDeletePopulateJail, downscaleAndOverwritePopulateJail, downscale, none, skipPopulateJail."
  }
}

variable "maintenance_ignore_node_groups" {
  description = "List of node groups that Soperator should ignore for maintenance events. Supported values: controller, nfs, system, login, accounting."
  type        = list(string)
  default     = ["controller", "nfs"]
}

# endregion Maintenance

# endregion Slurm

# region ActiveChecks
variable "active_checks_scope" {
  type        = string
  description = "Scope of active checks. Defines what active checks should be checked during cluster bootstrap."
  default     = "prod_quick"
  validation {
    condition     = contains(["dev", "testing", "prod_quick", "prod_acceptance", "essential"], var.active_checks_scope)
    error_message = "active_checks_scope must be one of: dev, testing, prod_quick, prod_acceptance, essential."
  }
}

# endregion ActiveChecks
