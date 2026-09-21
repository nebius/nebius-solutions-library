#----------------------------------------------------------------------------------------------------------------------#
#                                                                                                                      #
#                                                                                                                      #
#                                              Terraform - example values                                              #
#                                                                                                                      #
#                                                                                                                      #
#----------------------------------------------------------------------------------------------------------------------#

# Name of the company. It is used for context name of the cluster in .kubeconfig file.
company_name = ""

# Whether the cluster is production or not.
production = true

# Follow the installation guide and put IAM merge request URL here.
# Required if production = true.
iam_merge_request_url = ""

#----------------------------------------------------------------------------------------------------------------------#
#                                                                                                                      #
#                                                                                                                      #
#                                                    Infrastructure                                                    #
#                                                                                                                      #
#                                                                                                                      #
#----------------------------------------------------------------------------------------------------------------------#
# region Infrastructure

#----------------------------------------------------------------------------------------------------------------------#
#                                                                                                                      #
#                                                        Storage                                                       #
#                                                                                                                      #
#----------------------------------------------------------------------------------------------------------------------#
# region Storage

# Whether to store the controller state on filestore or network SSD.
controller_state_on_filestore = false

# Shared filesystem to be used on controller nodes.
# Deprecated: Starting with version 1.22, this variable isn't used, as controller state is stored on network SSD disks.
# Remains for the backward compatibility.
# ---
filestore_controller_spool = {
  spec = {
    size_gibibytes       = 128
    block_size_kibibytes = 4
    forbid_deletion      = false
  }
}
# Or use existing filestore.
# ---
# filestore_controller_spool = {
#   existing = {
#     id = "computefilesystem-<YOUR-FILESTORE-ID>"
#   }
# }

# Shared filesystem to be used on controller, worker, and login nodes.
# Notice that auto-backups are enabled for filesystems with size less than 12 TiB.
# If you need backups for jail larger than 12 TiB, set 'backups_enabled' to 'force_enable' down below.
# ---
# filestore_jail = {
#   spec = {
#     size_gibibytes       = 2048
#     block_size_kibibytes = 4
#     forbid_deletion      = false
#   }
# }
# Or use existing filestore.
# ---
filestore_jail = {
  existing = {
    id = "computefilesystem-<YOUR-FILESTORE-ID>"
  }
}

# Additional shared filesystems to be mounted inside jail.
# If a big filesystem is needed it's better to deploy this additional storage because jails bigger than 12 TiB
# ARE NOT BACKED UP by default.
# ---
# filestore_jail_submounts = [{
#   name       = "data"
#   mount_path = "/mnt/data"
#   spec = {
#     size_gibibytes       = 2048
#     block_size_kibibytes = 4
#     forbid_deletion      = false
#   }
# }]
# Or use existing filestores.
# ---
filestore_jail_submounts = [{
  name       = "data"
  mount_path = "/mnt/data"
  existing = {
    id = "computefilesystem-<YOUR-FILESTORE-ID>"
  }
}]


# Shared filesystem to be used for accounting DB.
# By default, null.
# Required if accounting_enabled is true.
# ---
filestore_accounting = {
  spec = {
    size_gibibytes       = 512
    block_size_kibibytes = 4
    forbid_deletion      = false
  }
}
# Or use existing filestore.
# ---
# filestore_accounting = {
#   existing = {
#     id = "computefilesystem-<YOUR-FILESTORE-ID>"
#   }
# }

# endregion Storage

# region nfs-server

# nfs = {
#   enabled        = false
#   size_gibibytes = 3720
#   mount_path     = "/home"
#   resource = {
#     platform = "cpu-d3"
#     preset   = "32vcpu-128gb"
#   }
#   public_ip = false
# }

nfs_in_k8s = {
  enabled         = true
  version         = "1.2.0"
  use_stable_repo = true
  size_gibibytes  = 3720
  disk_type       = "NETWORK_SSD_IO_M3"
  filesystem_type = "ext4"
  threads         = 32 # to match preset in slurm_nodeset_nfs
}

# endregion nfs-server

#----------------------------------------------------------------------------------------------------------------------#
#                                                                                                                      #
#                                                                                                                      #
#                                                         Slurm                                                        #
#                                                                                                                      #
#                                                                                                                      #
#----------------------------------------------------------------------------------------------------------------------#
# region Slurm

# Version of soperator.
# ---
slurm_operator_version = "4.1.8"

# Is the version of soperator stable or not.
# ---
slurm_operator_stable = true

# Each partition must have either is_all = true (includes all generated Slurm NodeSets)
# or slurm_nodeset_refs (list of specific generated Slurm NodeSet names).
# For GB300, one Terraform worker nodeset can produce multiple Slurm NodeSets:
# Terraform worker `primtrain` with size 36 generates `primtrain-rack0`
# and `primtrain-rack1`.
# Users must not remove the "hidden" partition.
# Users can modify the "main" partition, but should not remove it (there must be at least one default partition).
# ---
slurm_nodesets_partitions = [
  {
    name   = "main"
    is_all = true
    # e.g. ["worker"] or ["primtrain-rack0"]; set is_all = false when using refs.
    slurm_nodeset_refs = []
    config             = "Default=YES PriorityTier=10 PreemptMode=OFF MaxTime=INFINITE State=UP OverSubscribe=YES"
  },
  {
    name               = "hidden"
    is_all             = true
    slurm_nodeset_refs = []
    config             = "Default=NO PriorityTier=10 PreemptMode=OFF Hidden=YES MaxTime=INFINITE State=UP OverSubscribe=YES"
  },
]

# Type of the Slurm partition config. Could be either `default` or `custom`.
# By default, "default".
# ---
slurm_partition_config_type = "default"
# Partition config in case of `custom` slurm_partition_config_type.
# Each string must be started with `PartitionName`.
# By default, empty list.
# ---
# slurm_partition_raw_config = [
#   "PartitionName=low_priority Nodes=low_priority Default=YES MaxTime=INFINITE State=UP PriorityTier=1",
#   "PartitionName=high_priority Nodes=low_priority Default=NO MaxTime=INFINITE State=UP PriorityTier=2"
# ]
# If Nodes present, they must not contain node names: use only Slurm NodeSet values, "ALL" or "".
# If nodesets are used in the partition config, slurm_worker_features with non-empty nodeset_name
# must be declared (see below).
# Specifying specific nodes is not supported since Dynamic Nodes are used.
# For more details, see https://slurm.schedmd.com/dynamic_nodes.html#partitions.

# List of features to be enabled on worker nodes. Each feature object has:
# - name: (Required) The name of the feature.
# - hostlist_expr: (Required) A Slurm hostlist expression, e.g. "workers-[0-2,10],workers-[3-5]".
#   Soperator will run these workers with the feature name.
# - nodeset_name: (Optional) The Slurm nodeset name to be provisioned using this feature.
#   This nodeset may be used in conjunction with partitions.
#   It is required if `Nodes=<nodeset_name>` is used for a partition.
#

# Health check config:
# - health_check_interval: (Required) Interval for health check run in seconds.
# - health_check_program: (Required) Program for health check run.
# - health_check_node_state: (Required) What node states should execute the program.
#
# slurm_health_check_config = {
#   health_check_interval: 30,
#   health_check_program: "/usr/bin/gpu_healthcheck.sh",
#   health_check_node_state: [
#     {
#       state: "ANY"
#     },
#     {
#       state: "CYCLE"
#     }
#   ]
# }

#----------------------------------------------------------------------------------------------------------------------#
#                                                                                                                      #
#                                                         Nodes                                                        #
#                                                                                                                      #
#----------------------------------------------------------------------------------------------------------------------#
# region Nodes

# Configuration of System node set for system resources created by Soperator.
# Keep in mind that the k8s nodegroup will have auto-scaling enabled and the actual number of nodes depends on the size
# of the cluster.
# ---
slurm_nodeset_system = {
  min_size = 3
  max_size = 9
  resource = {
    platform = "cpu-d3"
    preset   = "8vcpu-32gb"
  }
  boot_disk = {
    type                 = "NETWORK_SSD"
    size_gibibytes       = 192
    block_size_kibibytes = 4
  }
}

# Configuration of Slurm Controller node set.
# ---
slurm_nodeset_controller = {
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

# Configuration of Slurm Worker node sets.
# Multiple worker nodesets are supported with different hardware configurations.
# Each nodeset will be automatically split into fixed-size node groups.
# GB300 workers must use size divisible by 18 in production.
# Non-production GB300 clusters may use one partial rack with size less than 18.
# Generated GB300 mk8s node groups are rack-sized, with 18 nodes except for the non-production partial-rack case.
# Their effective nodeset prefixes are generated as <name>-rack<rack>.
# For example, a GB300 worker named "primtrain" creates primtrain-rack0-0..primtrain-rack0-17
# for the first rack and primtrain-rack1-0..primtrain-rack1-17 for the second rack.
# Non-GB300 workers use the configured name as the node prefix, producing <name>-# nodes,
# and must not enable NVLink.
# Set gpu_cluster.id to attach workers to an existing GPU cluster.
# If id is omitted, infiniband_fabric is used to create a new GPU cluster.
# ---
slurm_nodeset_workers = [
  {
    name = "worker"
    size = 128
    # Autoscaling configuration. Set enabled = false to use fixed node count instead.
    autoscaling = {
      enabled = true
      # min_size options:
      # - null: min=max, no scale-down (default, recommended - saves ~10 min on initial provisioning)
      #   it can be changed to a number later if needed.
      # - N: can scale down to N nodes
      min_size = null
    }
    resource = {
      platform = "gpu-h100-sxm"
      preset   = "8gpu-128vcpu-1600gb"
    }
    boot_disk = {
      type                 = "NETWORK_SSD"
      size_gibibytes       = 512
      block_size_kibibytes = 4
    }
    gpu_cluster = {
      # id                = "gpucluster-..."
      infiniband_fabric = ""
    }
    # Change to preemptible = {} in case you want to use preemptible nodes
    preemptible = null
    # Use reservation_policy to leverage compute reservations (capacity blocks)
    # reservation_policy = {
    #   policy          = "AUTO"  # AUTO, FORBID, or STRICT
    #   reservation_ids = ["capacityblockgroup-xYYzzzzzz"]
    # }
    # Required for GB300 workers. This creates one NVLink instance group per node group
    # and labels nodes with nebius.com/nvlink-instance-group=<group-id>.
    # nvlink = {
    #   enabled = true
    #   type    = "GB300"
    # }
    # Optional mk8s placement policy node list for this nodeset. Non-production only.
    # placement_policy_nodes = []
    # Provide a list of strings to set Slurm Node features
    features = null
    # Set to `true` to create partition for the NodeSet by default
    create_partition = null
    # Whether to enable ephemeral nodes behavior for this worker nodeset.
    # When true, nodes will use dynamic topology injection and power management.
    # By default, false.
    ephemeral_nodes                = false
    initial_number_ephemeral_nodes = 1
    # Optional PersistentVolumeClaim retention policy for PVCs created by the worker nodeset StatefulSet.
    # Supported values: `Retain` or `Delete`.
    persistent_volume_claim_retention_policy = {
      when_deleted = "Delete"
      when_scaled  = "Delete"
    }
    # Optional local NVMe passthrough for this nodeset only.
    # Uses local instance disks, creates a RAID0 array and mounts it on the host via cloud-init.
    # mount_path: path used for both host RAID mount and jail submount.
    # local_nvme = {
    #   enabled         = true
    #   mount_path      = "/mnt/local-nvme"
    #   filesystem_type = "ext4"
    # }
    # Additional (Optional) node-local Network-SSD disks to be mounted inside jail on worker nodes.
    # It will create compute disks with provided spec for each node via CSI.
    # NOTE: in case of `NETWORK_SSD_NON_REPLICATED` disk type, `size` must be divisible by 93Gi - https://docs.nebius.com/compute/storage/types#disks-types.
    # ---
    # node_local_jail_submounts = []
    # ---
    node_local_jail_submounts = [{
      name            = "local-data"
      mount_path      = "/mnt/local-data"
      size_gibibytes  = 1024
      disk_type       = "NETWORK_SSD"
      filesystem_type = "ext4"
    }]
    # Whether to create extra NRD disks for storing Docker/Enroot images and container filesystems on each worker node.
    # It will create compute disks with provided spec for each node via CSI.
    # NOTE: In case you're not going to use Docker/Enroot in your workloads, it's worth disabling this feature.
    # NOTE: `size` must be divisible by 93Gi - https://docs.nebius.com/compute/storage/types#disks-types.
    # ---
    # node_local_image_disk = {
    #   enabled = false
    # }
    # ---
    node_local_image_disk = {
      enabled = true
      spec = {
        size_gibibytes  = 930
        filesystem_type = "ext4"
        # Could be changed to `NETWORK_SSD_NON_REPLICATED`
        disk_type = "NETWORK_SSD_IO_M3"
      }
    }
  },
]

# Per-platform CUDA versions consumed by Slurm/operator (e.g., 12.9.0). Keys are platform IDs (e.g., gpu-h100-sxm).
#platform_cuda_versions = {}

# Per-platform GPU driver presets. Keys are platform IDs (e.g., gpu-h100-sxm); values are driver presets (e.g., cuda13.0).
#platform_driver_presets = {}

# Driverfull mode is used to run Slurm jobs with GPU drivers installed on the worker nodes.
use_preinstalled_gpu_drivers = true

# Configuration of Slurm Login node set.
# Keep size as the desired login pod replica count. For GB300, Terraform uses
# this value for Soperator login pods, then skips only the dedicated mk8s login
# node group so login pods run on worker nodes instead.
# ---
slurm_nodeset_login = {
  size = 2
  # Optional. For GB300, this is set to false internally so login pods run on worker nodes.
  # node_group_enabled = true
  resource = {
    platform = "cpu-d3"
    preset   = "32vcpu-128gb"
  }
  boot_disk = {
    type                 = "NETWORK_SSD"
    size_gibibytes       = 256
    block_size_kibibytes = 4
  }
}

# Configuration of Slurm Accounting node set.
# Required in case of Accounting usage.
# By default, null.
# ---
slurm_nodeset_accounting = {
  resource = {
    platform = "cpu-d3"
    preset   = "8vcpu-32gb"
  }
  boot_disk = {
    type                 = "NETWORK_SSD"
    size_gibibytes       = 128
    block_size_kibibytes = 4
  }
}

# Configuration of NFS node set.
# ---
slurm_nodeset_nfs = {
  size = 1
  resource = {
    platform = "cpu-d3"
    preset   = "32vcpu-128gb"
  }
  boot_disk = {
    type                 = "NETWORK_SSD"
    size_gibibytes       = 128
    block_size_kibibytes = 4
  }
}

#----------------------------------------------------------------------------------------------------------------------#
#                                                         Login                                                        #
#----------------------------------------------------------------------------------------------------------------------#
# region Login

# Public or private ip for login node load balancer
# By default, true (public).
# ---
slurm_login_public_ip = true

# Whether to enable Tailscale init container on login pod.
# By default, false
# ---
tailscale_enabled = false

# Whether to enable the SSSD sidecar on Slurm controller, login, and worker nodes.
# By default, false
# ---
slurm_sssd_enabled = false

# Name of Secret containing sssd.conf for controller, login, and worker sssd containers.
# By default, empty
# ---
slurm_sssd_conf_secret_ref_name = ""

# Name of ConfigMap containing LDAP CA certificates for controller, login, and worker sssd containers.
# By default, empty
# ---
slurm_sssd_ldap_ca_config_map_ref_name = ""

# Authorized keys accepted for connecting to Slurm login nodes via SSH as 'root' user.
# ---
slurm_login_ssh_root_public_keys = [
  "",
]

# endregion Login

#----------------------------------------------------------------------------------------------------------------------#
#                                                       Exporter                                                       #
#----------------------------------------------------------------------------------------------------------------------#
# region Exporter

# Whether to enable Slurm metrics exporter.
# By default, true.
# ---
slurm_exporter_enabled = true

# endregion Exporter

#----------------------------------------------------------------------------------------------------------------------#
#                                                      ActiveChecks                                                    #
#----------------------------------------------------------------------------------------------------------------------#
# region ActiveChecks

# Scope of active health-checks. Defines what checks should run after the cluster is provisioned.
# Available scopes:
# - "prod_acceptance" - run all available health-checks. Takes additional 30 minutes (H100) - 2 hours (B300).
# - "prod_quick" - run all health-checks except those that take long. Takes additional 10 minutes (H100) - 30 minutes (B300).
# - "testing" - to be used for Soperator E2E tests.
# - "dev" - to be used for Soperator development clusters.
# - "essential" - skip most of checks and run only essential ones. Don't use in production.
# ---
active_checks_scope = ""

# endregion ActiveChecks

# endregion Nodes

#----------------------------------------------------------------------------------------------------------------------#
#                                                                                                                      #
#                                                        Config                                                        #
#                                                                                                                      #
#----------------------------------------------------------------------------------------------------------------------#
# region Config

# Shared memory size for Slurm controller and worker nodes in GiB.
# By default, 64.
# ---
slurm_shared_memory_size_gibibytes = 1024

# Block size for Slurm topology/block topology plugin in number of nodes.
# This affects how Slurm groups nodes into blocks for scheduling purposes.
# A smaller block size allows for more flexible scheduling but may increase overhead,
# while a larger block size may improve scheduling efficiency but reduce flexibility.
# The optimal value depends on the cluster size and workload characteristics.
#
# By default, null (no block topology plugin configuration applied).
#
# For GB300,
# it is recommended to set block size to the rack size (18) or its multiple to optimize for rack-level scheduling.
# ---
# slurm_topology_block_size = 18
# ---
slurm_topology_block_size = null

# Node groups that Soperator should ignore during maintenance events.
# These ignored maintenance events will be handled by mk8s control plane instead.
# Supported values: controller, nfs, system, login, accounting.
# ---
maintenance_ignore_node_groups = ["controller", "nfs"]

# endregion Config
#----------------------------------------------------------------------------------------------------------------------#
#                                                                                                                      #
#                                                       Telemetry                                                      #
#                                                                                                                      #
#----------------------------------------------------------------------------------------------------------------------#
# region Telemetry

# Whether to enable telemetry.
# By default, true.
# ---
telemetry_enabled = true

# Whether to enable dcgm job mapping (adds hpc_job label on DCGM_ metrics).
# By default, true.
# ---
dcgm_job_mapping_enabled = true

# Configuration of the Soperator Notifier (https://github.com/nebius/soperator/tree/main/helm/soperator-notifier).
# ---
# soperator_notifier = {
#   enabled           = true
#   slack_webhook_url = "https://hooks.slack.com/services/X/Y/Z"
# }
soperator_notifier = {
  enabled = false
}

public_o11y_enabled = true

# endregion Telemetry

#----------------------------------------------------------------------------------------------------------------------#
#                                                                                                                      #
#                                                       Accounting                                                     #
#                                                                                                                      #
#----------------------------------------------------------------------------------------------------------------------#
# region Accounting

# Whether to enable Accounting.
# By default, true.
# ---
accounting_enabled = true

# endregion Accounting

# endregion Slurm

#----------------------------------------------------------------------------------------------------------------------#
#                                                                                                                      #
#                                                       Backups                                                        #
#                                                                                                                      #
#----------------------------------------------------------------------------------------------------------------------#
# region Backups

# Whether to enable Backups. Choose from 'auto', 'force_enable', 'force_disable'.
# 'auto' turns backups on for jails with max size less than 12 TB and is a default option.
# ---
backups_enabled = "auto"

# Password to be used for encrypting jail backups.
# ---
backups_password = "password"

# Cron schedule for backup task.
# See https://docs.k8up.io/k8up/references/schedule-specification.html for more info.
# ---
backups_schedule = "@daily-random"

# Cron schedule for prune task (when old backups are discarded).
# See https://docs.k8up.io/k8up/references/schedule-specification.html for more info.
# ---
backups_prune_schedule = "@daily-random"

# Backups retention policy - how many last automatic backups to save.
# Helps to save storage and to get rid of old backups as they age.
# Manually created backups (without autobackup tag) are not discarded.
#
# You can set keepLast, keepHourly, keepDaily, keepWeekly, keepMonthly and keepYearly.
# ---
backups_retention = {
  # How many daily snapshots to save.
  # ---
  keepDaily = 7
}

# Whether to delete on destroy all backup data from bucket or not.
cleanup_bucket_on_destroy = false

# endregion Backups

#----------------------------------------------------------------------------------------------------------------------#
#                                                                                                                      #
#                                                      Kubernetes                                                      #
#                                                                                                                      #
#----------------------------------------------------------------------------------------------------------------------#
# region k8s

# Version of the k8s to be used.
# Set to null or don't set to use Nebius default (recommended), or specify explicitly
# ---
k8s_version = 1.35

# SSH user credentials for accessing k8s nodes.
# That option add public ip address to every node.
# By default, empty list.
# ---
# k8s_cluster_node_ssh_access_users = [{
#   name = "<USER1>"
#   public_keys = [
#     "<ENCRYPTION-METHOD1 HASH1 USER1>",
#     "<ENCRYPTION-METHOD2 HASH2 USER1>",
#   ]
# }]

# Lines to write to /etc/modprobe.d/nvidia_config.conf via cloud-init (GPU workers only).
# One option per line.
# ---
nvidia_config_lines = [
  "options nvidia NVreg_RestrictProfilingToAdminUsers=0", # Allow access to GPU counters in nsys profiler for non-root users
  "options nvidia NVreg_EnableStreamMemOPs=1",
  "options nvidia NVreg_RegistryDwords=\"PeerMappingOverride=1;\"",
]

# endregion k8s
