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

variable "iam_token" {
  description = "IAM token used for communicating with Nebius services."
  type        = string
  nullable    = false
  sensitive   = true
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

variable "o11y_iam_project_id" {
  description = "ID of the IAM project for O11y."
  type        = string
  nullable    = false

  validation {
    condition     = startswith(var.o11y_iam_project_id, "project-")
    error_message = "ID of the IAM project must start with `project-`."
  }
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

variable "o11y_iam_group_id" {
  description = "ID of the IAM group for O11y."
  type        = string
  nullable    = false

  validation {
    condition     = startswith(var.o11y_iam_group_id, "group-")
    error_message = "ID of the IAM group must start with `group-`."
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

variable "filestore_controller_spool" {
  description = "Shared filesystem to be used on controller nodes."
  type = object({
    existing = optional(object({
      id = string
    }))
    spec = optional(object({
      size_gibibytes       = number
      block_size_kibibytes = number
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
}

variable "node_local_jail_submounts" {
  description = "Node-local disks to be mounted inside jail on worker nodes."
  type = list(object({
    name            = string
    mount_path      = string
    size_gibibytes  = number
    disk_type       = string
    filesystem_type = string
  }))
  nullable = false
  default  = []

  validation {
    condition = alltrue([
      for sm in var.node_local_jail_submounts : (
        contains(
          [
            module.resources.disk_types.network_ssd,
            module.resources.disk_types.network_ssd_non_replicated,
            module.resources.disk_types.network_ssd_io_m3,
          ],
          sm.disk_type
        )
    )])
    error_message = "Disk type must be one of `NETWORK_SSD`, `NETWORK_SSD_NON_REPLICATED` or `NETWORK_SSD_IO_M3`. See https://docs.nebius.com/compute/storage/types#disks-types"
  }
  validation {
    condition = alltrue([
      for sm in var.node_local_jail_submounts : (
        contains(
          [
            module.resources.filesystem_types.ext4,
            module.resources.filesystem_types.xfs,
          ],
          sm.filesystem_type
        )
    )])
    error_message = "Filesystem type must be one of `ext4` or `xfs`."
  }
}

variable "node_local_image_disk" {
  description = "Whether to create extra NRD/IO M3 disks for storing Docker/Enroot images and container filesystems on each worker node."
  type = object({
    enabled = bool
    spec = optional(object({
      size_gibibytes  = number
      filesystem_type = string
      disk_type       = string
    }))
  })
  default = {
    enabled = false
  }

  validation {
    condition = (var.node_local_image_disk.enabled
      ? var.node_local_image_disk.spec != null
      : true
    )
    error_message = "Spec must be provided if enabled."
  }
  validation {
    condition = (var.node_local_image_disk.spec == null
      ? true
      : (contains(
        [
          module.resources.filesystem_types.ext4,
          module.resources.filesystem_types.xfs,
        ],
        var.node_local_image_disk.spec.filesystem_type
      ))
    )
    error_message = "Filesystem type must be one of `ext4` or `xfs`."
  }
  validation {
    condition = (var.node_local_image_disk.spec == null
      ? true
      : (contains(
          [
            module.resources.disk_types.network_ssd_non_replicated,
            module.resources.disk_types.network_ssd_io_m3,
          ],
          var.node_local_image_disk.spec.disk_type
        ))
    )
    error_message = "Local image disk type must be one of `NETWORK_SSD_NON_REPLICATED` or `NETWORK_SSD_IO_M3`. See https://docs.nebius.com/compute/storage/types#disks-types"
  }
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

variable "k8s_cluster_node_ssh_access_users" {
  description = "SSH user credentials for accessing k8s nodes."
  type = list(object({
    name        = string
    public_keys = list(string)
  }))
  nullable = false
  default  = []
}

variable "etcd_cluster_size" {
  description = "Size of the etcd cluster."
  type        = number
  default     = 3
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

# region WorkerFeatures

variable "slurm_worker_features" {
  description = "List of features to be enabled on worker nodes."
  type = list(object({
    name          = string
    hostlist_expr = string
    nodeset_name  = optional(string)
  }))
  default = []
}

# endregion WorkerFeatures

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
    min_size = 3
    max_size = 9
    resource = {
      platform = "cpu-d3"
      preset   = "16vcpu-64gb"
    }
    boot_disk = {
      type                 = "NETWORK_SSD"
      size_gibibytes       = 128
      block_size_kibibytes = 4
    }
  }
}

variable "slurm_nodeset_controller" {
  description = "Configuration of Slurm Controller node set."
  type = object({
    size = number
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
      size_gibibytes       = 128
      block_size_kibibytes = 4
    }
  }
}

variable "slurm_nodeset_workers" {
  description = "Configuration of Slurm Worker node sets."
  type = list(object({
    size                    = number
    nodes_per_nodegroup     = number
    max_unavailable_percent = number
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
      infiniband_fabric = string
    }))
  }))
  nullable = false
  default = [{
    size                    = 1
    nodes_per_nodegroup     = 1
    max_unavailable_percent = 50
    resource = {
      platform = "cpu-d3"
      preset   = "16vcpu-64gb"
    }
    boot_disk = {
      type                 = "NETWORK_SSD"
      size_gibibytes       = 128
      block_size_kibibytes = 4
    }
  }]

  # TODO: change to `>0` when node sets supported in soperator
  validation {
    condition     = length(var.slurm_nodeset_workers) == 1
    error_message = "Only one worker node set must be provided for a while."
  }

  validation {
    condition = alltrue([
      for worker in var.slurm_nodeset_workers :
      (worker.size % worker.nodes_per_nodegroup == 0)
    ])
    error_message = "Worker count must be divisible by nodes_per_nodegroup."
  }
}

variable "slurm_nodeset_login" {
  description = "Configuration of Slurm Login node set."
  type = object({
    size = number
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
      size_gibibytes       = 128
      block_size_kibibytes = 4
    }
  }
}

variable "slurm_nodeset_accounting" {
  description = "Configuration of Slurm Accounting node set."
  type = object({
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
  nullable = true
  default  = null
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

resource "terraform_data" "check_slurm_nodeset" {
  for_each = merge({
    "system"     = var.slurm_nodeset_system
    "controller" = var.slurm_nodeset_controller
    "login"      = var.slurm_nodeset_login
    }, { for i, worker in var.slurm_nodeset_workers :
    "worker_${i}" => worker
  })

  depends_on = [
    terraform_data.check_region,
  ]

  lifecycle {
    precondition {
      condition = (
        try(each.value.size, 0) > 0 ||
        try(each.value.min_size, 0) > 0
      )
      error_message = "Either size or min_size must be greater than zero in node set ${each.key}."
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

variable "slurm_login_ssh_root_public_keys" {
  description = "Authorized keys accepted for connecting to Slurm login nodes via SSH as 'root' user."
  type        = list(string)
  nullable    = false
}

# endregion Login

# region Exporter

variable "slurm_exporter_enabled" {
  description = "Whether to enable Slurm metrics exporter."
  type        = bool
  default     = true
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
}

variable "default_prolog_enabled" {
  description = "Whether to enable default Slurm Prolog script that drain nodes with bad GPUs."
  type        = bool
  default     = true
}

variable "default_epilog_enabled" {
  description = "Whether to enable default Slurm Epilog script that drain nodes with bad GPUs."
  type        = bool
  default     = true
}

# endregion Config

# region NCCL benchmark

variable "nccl_benchmark_enable" {
  description = "Whether to enable NCCL benchmark CronJob to benchmark GPU performance. It won't take effect in case of 1-GPU hosts."
  type        = bool
  default     = true
}

variable "nccl_benchmark_schedule" {
  description = "NCCL benchmark's CronJob schedule."
  type        = string
  default     = "0 */3 * * *"
}

variable "nccl_benchmark_min_threshold" {
  description = "Minimal threshold of NCCL benchmark for GPU performance to be considered as acceptable."
  type        = number
  default     = 420
}

variable "nccl_use_infiniband" {
  description = "Use infiniband defines using NCCL_P2P_DISABLE=1 NCCL_SHM_DISABLE=1 NCCL_ALGO=Ring env variables for test."
  type        = bool
  default     = false
}

# endregion NCCL benchmark

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

variable "dcgm_job_mapping_enabled" {
  description = "Whether to enable HPC job mapping by installing a separate dcgm-exporter"
  type        = bool
  default     = true
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
  description = "Slurm.conf accounting configuration. See https://slurm.schedmd.com/slurm.conf.html. Not all options are supported."
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
  sensitive   = true
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

# endregion Maintenance

# endregion Slurm

# region fluxcd
variable "github_org" {
  description = "The GitHub organization."
  type        = string
  default     = "nebius"
}

variable "github_repository" {
  description = "The GitHub repository."
  type        = string
  default     = "soperator"
}

variable "flux_interval" {
  description = "The interval for Flux to check for changes."
  type        = string
  default     = "1m"
}

# endregion fluxcd
