#----------------------------------------------------------------------------------------------------------------------#
#                                                                                                                      #
#                                                                                                                      #
#                                     Soperator Pro - Terraform Variables Template                                     #
#                                                                                                                      #
#                                                                                                                      #
#----------------------------------------------------------------------------------------------------------------------#

# Name of the company.
# Used for prefixing names of the Mk8s cluster and other resources.
company_name = ""

# Whether the cluster is production or not.
production = true

# Cloud API environment, independent of whether this is a production deployment.
# Testing uses beta/omega regions and the testing CLI profile.
cloud_environment = "production"

# Follow the installation guide and put IAM merge request URL here.
# Required if production = true.
iam_merge_request_url = ""

# Soperator version.
# Don't change the default value without a reason.
slurm_operator_version = "4.1.13"

# Whether the Soperator version is stable or not.
# Used for selecting the container registry for pulling Helm charts and container images.
slurm_operator_stable = true



#----------------------------------------------------------------------------------------------------------------------#
#                                                                                                                      #
#                                                  Shared Filesystems                                                  #
#                                                                                                                      #
#----------------------------------------------------------------------------------------------------------------------#

# Shared root filesystem (a.k.a. jail) holding the cluster environment.
filesystem_jail = {
  existing = {
    id = "computefilesystem-***"
  }
}
# To make Terraform create a new filesystem:
# ---
# filesystem_jail = {
#   spec = {
#     type                 = "NETWORK_SSD"
#     size_gibibytes       = 2048
#     block_size_kibibytes = 4
#     forbid_deletion      = true
#   }
# }

# Additional shared filesystems for storing large volumes of data (datasets, checkpoints, etc.).
#
# WARNING: You can't use "/home" mount path if Unix NFS is enabled.
# NOTE: Node-local filesystems (network SSD / local NVMe) are configured in the NodeSets section below.
filesystem_jail_submounts = [{
  name       = "data"
  mount_path = "/data"
  existing = {
    id = "computefilesystem-***"
  }
}]
# To make Terraform create new filesystems:
# ---
# filesystem_jail_submounts = [{
#   name       = "data"
#   mount_path = "/data"
#   spec = {
#     type                 = "NETWORK_SSD"
#     size_gibibytes       = 2048
#     block_size_kibibytes = 4
#     forbid_deletion      = true
#   }
# }]

# Unix NFS server to be mounted to "/home".
# This is a non-parallel shared filesystem and it can't be used for storing datasets or checkpoints.
# It can be used only storing code, configs, container images, and so on.
#
# WARNING: Unix NFS doesn't work on huge clusters (> 1000 nodes) and can't be used there. When it's
# disabled, the content of the "/home" directory is stored on jail.
nfs_in_k8s = {
  enabled = true
  spec = {
    version         = "1.2.2"
    use_stable_repo = true
    size_gibibytes  = 3720
    disk_type       = "NETWORK_SSD_IO_M3"
    filesystem_type = "ext4"
    threads         = 128
    node_group = {
      resource = {
        platform = "cpu-d3"
        # preset omitted -> driven by sizing_tier. Set a preset to override.
      }
      # boot_disk omitted -> defaults are used.
    }
  }
}



#----------------------------------------------------------------------------------------------------------------------#
#                                                                                                                      #
#                                         Worker Nodes and Slurm Configuration                                         #
#                                                                                                                      #
#----------------------------------------------------------------------------------------------------------------------#

# Worker nodes are Slurm compute nodes defined via one or more "NodeSets".
#
# A NodeSet is a group of worker nodes with the same:
#   - compute platform and preset;
#   - node-local filesystems;
#   - soperator settings;
#   - name prefix (i.e. nodeset "worker" produces Slurm nodes "worker-0", "worker-1", ...).
#
# NodeSets work differently for GB300 and other GPU platforms:
#
#                     (non-GB300)                                            (GB300)
#
#                ┌───────────────────┐                                ┌───────────────────┐
#                │ Terraform NodeSet │                                │ Terraform NodeSet │
#                │     "worker"      │                                │     "worker"      │
#                └─────────┬─────────┘                                └─────────┬─────────┘
#                          │ 1 : N (per 100 nodes)                              │ 1 : N (per rack of 18)
#             ┌────────────┼─────────────┐                        ┌─────────────┼──────────────┐
#    ┌────────┴───────┐    │    ┌────────┴───────┐      ╔═════════╧════════╗    │    ╔═════════╧════════╗
#    │ Mk8s NodeGroup │   ...   │ Mk8s NodeGroup │      ║ NVLInstanceGroup ║   ...   ║ NVLInstanceGroup ║
#    └────────┬───────┘         └────────┬───────┘      ╚═════════╤════════╝         ╚═════════╤════════╝
#             └────────────┬─────────────┘               ┌────────┴───────┐           ┌────────┴───────┐
#                          │ N : 1 (all feed one CR)     │ Mk8s NodeGroup │           │ Mk8s NodeGroup │
#              ┌───────────┴──────────┐                  └────────┬───────┘           └────────┬───────┘
#              │ Soperator NodeSet CR │               ┌───────────┴──────────┐     ┌───────────┴──────────┐
#              └───────────┬──────────┘               │ Soperator NodeSet CR │     │ Soperator NodeSet CR │
#                  ┌───────┴───────┐                  └───────────┬──────────┘     └───────────┬──────────┘
#                  │ Slurm NodeSet │                     ┌────────┴───────┐           ┌────────┴───────┐
#                  │   "worker"    │                     │ Slurm NodeSet  │           │ Slurm NodeSet  │
#                  └───────┬───────┘                     │ "worker-rack0" │           │ "worker-rack1" │
#                          │ 1 : M                       └────────┬───────┘           └────────┬───────┘
#      ┌───────────┬───────┼────────┬───────────┐                 │                            │
# [worker-0]  [worker-1]  ...  [worker-k]  [worker-M]        ┌────┘                            └────┐
#                                                            │ 1 : 18                               │ 1: 18
#                                                ┌───────────┼───────────┐              ┌───────────┼───────────┐
#                                          [worker-rack0-0] ... [worker-rack0-17] [worker-rack1-0] ... [worker-rack1-17]
#
# NOTE: One _Terraform_ NodeSet corresponds to several _Slurm_ NodeSets for GB300 (one per rack).
#
# "Slurm NodeSet" is not a runtime entity, it's just a name for referencing in partitions.
# Users can also pass Slurm NodeSet names to Slurm commands ("sbatch", "scontrol", etc.) instead of full nodelists.
slurm_nodeset_workers = [
  {
    name = "worker"
    size = 128 # Must be divisable by 18 for GB300.

    #------------------------------------------------#
    #                    Resources                   #
    #------------------------------------------------#
    resource = {
      platform = "gpu-b300-sxm"
      preset   = "8gpu-192vcpu-2768gb"
    }
    # boot_disk omitted -> defaults are used.
    gpu_cluster = {
      infiniband_fabric = "" # Use "id" instead of "infiniband_fabric" to attach workers to an existing GPU cluster.
    }
    nvlink = {
      enabled = false # Must be enabled for GB300 NodeSets.
      type    = "GB300"
    }

    #------------------------------------------------#
    #                     Scaling                    #
    #------------------------------------------------#
    ephemeral_nodes                = true  # If enabled, users can suspend and resume workers using Slurm commands.
    auto_resume                    = false # Whether to automatically resume workers when there are jobs in the queue.
    initial_number_ephemeral_nodes = 1     # How many ephemeral nodes should be running when the cluster is created.
    autoscaling = {
      enabled  = true # If enabled, ephemeral node suspend/resume will lead to VM deletion/creation.
      min_size = 0    # Minimum NodeGroup size. If null, min_size=max_size (no scale-down).
    }

    #------------------------------------------------#
    #             Node-local filesystems             #
    #------------------------------------------------#
    # Local NVMe-backed kubelet ephemeral storage for this nodeset.
    # Defaults to enabled for gpu-gb300 and disabled for other platforms.
    #
    # WARNING: Local NVMe layout may differ by region, platform, preset, and fabric.
    local_nvme = {
      # enabled = true
      # device_count              = 8
      # device_capacity_gigabytes = 3840 # Decimal GB per device (1 GB = 10^9 bytes).
      # mount_path                = "/scratch"
      # size_limit_gibibytes      = 20000
    }

    # Optional node-local network disks to be mounted on worker nodes.
    #
    # NOTE: When disk_type = "NETWORK_SSD_NON_REPLICATED", size must be divisible by 93Gi.
    node_local_jail_submounts = [
      # {
      #   name            = "local-data"
      #   mount_path      = "/scratch"
      #   size_gibibytes  = 1024
      #   disk_type       = "NETWORK_SSD"
      #   filesystem_type = "ext4"
      # },
    ]

    # Whether to create node-local disks for storing Docker container runtime data on worker nodes.
    # If disabled, only Enroot containers will work.
    node_local_image_disk = {
      enabled = false
      # spec = {
      #   size_gibibytes  = 930 # Must be divisible by 93.
      #   filesystem_type = "ext4"
      #   disk_type       = "NETWORK_SSD_IO_M3" # Or "NETWORK_SSD_NON_REPLICATED"
      # }
    }

    #------------------------------------------------#
    #                Advanced settings               #
    #------------------------------------------------#
    reservation_policy = {
      policy = "AUTO" # Docs: https://docs.nebius.com/compute/virtual-machines/reservations#terraform
      # reservation_ids = ["capacityblockgroup-***"]
    }
    preemptible             = null                      # Set to {} to use preemptible VMs.
    placement_policy_nodes  = null                      # Optional list of bare-metal FQDNs where VMs can run.
    rolling_update_strategy = "slurmAwareRollingUpdate" # Whether to update nodes without interrupting Slurm jobs.
    drain_timeout           = "0s"                      # Graceful drain timeout for Mk8s nodegroups, 0 = no timeout.
    extra_labels            = {}                        # Additional K8s node labels.
    features                = null                      # List of strings to set Slurm node features.
    max_pods                = 32                        # Maximum number of pods per K8s node to reduce Pod CIDR usage.
    persistent_volume_claim_retention_policy = {
      when_deleted = "Delete" # Set to "Retain" to preserve PVCs on NodeSet deletion.
      when_scaled  = "Delete" # Set to "Retain" to preserve PVCs on NodeSet scaling or ephemeral node suspention.
    }
  },
]

# Slurm partition configuration.
#
# Each partition must have either is_all = true (includes all generated Slurm NodeSets)
# or slurm_nodeset_refs (list of specific generated Slurm NodeSet names).
#
# For GB300, one Terraform worker nodeset generates one Slurm nodeset per rack: e.g., a Terraform
# nodeset "worker" with 3 racks will produce 3 Slurm nodesets: "worker-rack0", "worker-rack1", "worker-rack2".
#
# The setting "topology" is required. You must select one of 3 available topologies:
# +-------------+--------------------------------------+-----------------+---------------------------
# | topology    | influence on scheduling              | available for   | best for                 |
# +-------------+--------------------------------------+-----------------+--------------------------+
# | flat        | no topology awareness                | any nodes       | CPU-only partitions      |
# | tree-ib     | IB locality influences scheduling    | any GPU nodes   | non-GB300 GPU partitions |
# | block-nvl72 | Rack locality influcenes scheduling  | GB300 nodes     | GB300 partitions         |
# +-------------+--------------------------------------+-----------------+--------------------------+
#
# You must not remove the "hidden" partition.
# There must be exactly one partition with Default=YES.
#
# NOTE: Automatic suspention of ephemeral nodes can be enabled per partition, by using "SuspendTime=<sec>" setting.
#
# Available Slurm partition settings: https://slurm.schedmd.com/slurm.conf.html#SECTION_PARTITION-CONFIGURATION
slurm_nodesets_partitions = [
  {
    name               = "main" # The default partition for user jobs, can be renamed, split, or modified.
    is_all             = true   # Set to false for selecting specific Slurm NodeSets.
    slurm_nodeset_refs = []     # The list of _Slurm_ NodeSet names (e.g. "worker", or "worker-rack0").
    topology           = ""     # Prefer using "flat" for CPU-only, "tree-ib" for GPU, "block-nvl72" for GB300.
    config             = "Default=YES PriorityTier=10 PreemptMode=OFF MaxTime=INFINITE State=UP OverSubscribe=YES"
  },
  {
    name   = "hidden" # The partition to be used by Soperator active checks. Don't modify.
    is_all = true
    config = "Default=NO PriorityTier=10 PreemptMode=OFF Hidden=YES MaxTime=INFINITE State=UP OverSubscribe=YES"
  },
]



#----------------------------------------------------------------------------------------------------------------------#
#                                                                                                                      #
#                                           Login Nodes and SSH Configuration                                          #
#                                                                                                                      #
#----------------------------------------------------------------------------------------------------------------------#

# Login nodes is where users appear when connect to the cluster.
# Their number and size depends on user needs.
#
# NOTE: For GB300 clusters, login pods live next to worker pods and dedicated login nodes aren't created. You can
# still change the number of login pods by tuning the "size" variable here.
slurm_nodeset_login = {
  size = 2
  resource = {
    platform = "cpu-d3"
    preset   = "32vcpu-128gb"
  }
  # boot_disk omitted -> defaults are used.
}

# SSH public keys for connecting to Slurm login nodes via SSH as "root" user.
#
# WARNING: This variable shouldn't be used for managing user SSH keys long-term. After connecting to the cluster, any
# person can create more linux users. This variable is needed only for connecting to the cluster after it's created.
slurm_login_ssh_root_public_keys = [
  "",
]

# Whether to create public IP for login load balancer.
slurm_login_public_ip = true

# Whether to enable Tailscale VPN.
tailscale_enabled = false

# Whether to enable SSSD for IAM integration.
slurm_sssd_enabled = false

# Name of K8s Secret containing sssd.conf. If empty, a safe, but meaningless config is used.
#
# NOTE: You should create the K8s Secret yourself, after the cluster is already created, and then re-apply.
slurm_sssd_conf_secret_ref_name = ""

# Name of K8s ConfigMap containing LDAP CA certificates. Should be set for self-signed CAs.
#
# NOTE: You should create the K8s ConfigMap yourself, after the cluster is already created, and then re-apply.
slurm_sssd_ldap_ca_config_map_ref_name = ""



#----------------------------------------------------------------------------------------------------------------------#
#                                                                                                                      #
#                                                   Optional Features                                                  #
#                                                                                                                      #
#----------------------------------------------------------------------------------------------------------------------#

# Soperator can start active checks after the cluster is created. Most of them check GPU/IB health.
#
# Available scopes:
# - "prod_acceptance" - run all available checks. Takes additional ~1 hour.
# - "prod_quick"      - run short GPU health checks. Takes additional ~30 minutes.
# - "essential"       - skip everything that can be skipped in production.
# - "skip_all"        - [Don't use in production] run only checks required for cluster initialization.
#
# WARNING: Terraform won't finish until all checks pass successfully, which can require manual retries from inside K8s.
active_checks_scope = "essential"

# Whether to send Slack notifications with job events.
# When enabled, you must provide the Slack webhook URL targeting to the Slack channel.
#
# NOTE: Notifications are sent only for jobs submitted with "--mail-user=<Slack member ID>".
# To get Slack member ID: Profile picture -> Profile -> ⋮ -> Copy member ID.
soperator_notifier = {
  enabled = false
  # slack_webhook_url = "https://hooks.slack.com/services/X/Y/Z"
}

# Whether to enable NCCL Inspector.
nccl_inspector_profiling = {
  enabled = false
  # dump_dir = "/opt/soperator-outputs/shared/nccl_profiles"
  # verbose  = false
}

# Whether to enable periodic backups of the jail filesystem.
#
# Possible values:
# - "auto"          - enables backups if the jail filesystem is <= 12 TiB.
# - "force_disable" - disable backups.
# - "force_enable"  - enable backups.
backups_enabled = "force_disable"

backups_password       = "password"      # The password for encrypting jail backups.
backups_schedule       = "@daily-random" # See https://docs.k8up.io/k8up/references/schedule-specification.html.
backups_prune_schedule = "@daily-random" # See https://docs.k8up.io/k8up/references/schedule-specification.html.

# Backups retention policy - how many backups to keep.
backups_retention = {
  keepDaily = 7 # Use one of: keepDaily, keepLast, keepHourly, keepWeekly, keepMonthly, keepYearly.
}

# Whether to delete all backups on "terraform destroy".
#
# WARNING: After changing this setting, you first need to run "terraform apply" before "terraform destroy".
cleanup_bucket_on_destroy = false

# User credentials for direct SSH access to K8s nodes.
#
# NOTE: By default, K8s nodes only have private IP addresses.
k8s_cluster_node_ssh_access_users = [
  # {
  #   name = "<user name>"
  #   public_keys = [
  #     "<SSH public key>",
  #   ]
  # }
]



#----------------------------------------------------------------------------------------------------------------------#
#                                                                                                                      #
#                                                   System Resources                                                   #
#                                                                                                                      #
#                                            (Don't change without a reason)                                           #
#                                                                                                                      #
#----------------------------------------------------------------------------------------------------------------------#

# Sizing tier override. The sizing tier is a single knob that scales all system/observability
# component resources (kruise, VM stack, SPO, collectors, REST, mariadb, ...) and CPU node
# presets by cluster size. null (default) auto-derives the tier from the worker node count;
# set "XS".."XL" to force it.
#
# Tier boundaries and per-tier values: soperator/modules/sizing_tier/main.tf.
sizing_tier_override = null

# Optional per-component overrides ON TOP of the sizing tier.
component_overrides = {}

# System nodes are used for hosting Soperator itself, in-cluster observability stack, Slurm REST API, and other
# infrastructure components that make the cluster work.
#
# NOTE: The Mk8s nodegroup has auto-scaling and the actual number of nodes isn't known in advance -- it
# depends on the cluster size, its configuration and applied customizations.
slurm_nodeset_system = {
  min_size = 3
  max_size = 24
  resource = {
    platform = "cpu-d3"
    # preset omitted -> driven by sizing_tier. Set a preset to override.
  }
  # boot_disk omitted -> defaults are used.
}

# Controller nodes are used for hosting Slurm controller.
slurm_nodeset_controller = {
  size = 1
  resource = {
    platform = "cpu-d3"
    # preset omitted -> driven by sizing_tier. Set a preset to override.
  }
  # boot_disk omitted -> defaults are used.
}

# Accounting nodes are used for hosting MariaDB and Slurm accounting daemon.
slurm_nodeset_accounting = {
  resource = {
    platform = "cpu-d3"
    # preset omitted -> driven by sizing_tier. Set a preset to override.
  }
  # boot_disk omitted -> defaults are used.
}

# Accounting MariaDB storage size.
accounting_storage_size_gibibytes = 512



#----------------------------------------------------------------------------------------------------------------------#
#                                                                                                                      #
#                                                   Advanced Settings                                                  #
#                                                                                                                      #
#                                            (Don't change without a reason)                                           #
#                                                                                                                      #
#----------------------------------------------------------------------------------------------------------------------#

# Base block size for the "block-nvl72" topology.
# Should be equal to the the size of NVL Instance Groups.
slurm_topology_block_size = 18

# Whether Mk8s nodes should be created with preinstalled GPU drivers (a.k.a. "driverfull" mode).
use_preinstalled_gpu_drivers = true

# Per-platform CUDA version overrides. Example: { gpu-h100-sxm = "12.8.5", gpu-b300-sxm = "13.0.3" }
#---
#platform_cuda_versions = {}

# Per-platform GPU driver preset overrides. Example: { gpu-h100-sxm = "cuda12.8", gpu-b300-sxm = "cuda13.0" }
#---
#platform_driver_presets = {}

# Shared memory size for worker nodes.
slurm_shared_memory_size_gibibytes = 1024

# Which NodeGroups should be excluded from Soperator maintenance event handling.
# Mk8s will handle them instead of Soperator.
maintenance_ignore_node_groups = ["controller", "nfs", "system", "accounting"]

# Whether to generate the default AppArmor profile in workers' cloud-init.
use_default_apparmor_profile = true

# The Soperator maintenance mode useful for repopulating jail filesystem or recreating all pods.
maintenance = "none"

# Whether to use squashfs mounting + overlayfs for Enroot containers instead of unpacking to node_local_image_disk.
enroot_direct_squashfs_enabled = true

# K8s ConfigMap name with the custom external SSHD configuration (login nodes).
slurm_login_sshd_config_map_ref_name = ""

# K8s ConfigMap name with the custom internal SSHD configuration (worker nodes).
slurm_worker_sshd_config_map_ref_name = ""

# Whether to enable telemetry.
telemetry_enabled = true

# Whether to install soperator's dcgm-exporter chart.
# Can be disabled on driverless setups since the GPU operator comes with its own DCGM exporter.
dcgm_exporter_enabled = true

# Maximum number of concurrent collections per collector in Slurm exporter.
#
# NOTE: Increasing this value may cause OOM issues on Slurm REST component, so it is recommended to increase REST
# node resources as well.
slurm_exporter_max_collector_inflight = 1

# Optional kube-state-metrics scrape size override in bytes.
# By default, it is raised automatically for large clusters.
kube_state_metrics_max_scrape_size = null

# Optional OpenTelemetry sending_queue batch overrides for the in-cluster (VictoriaLogs/VictoriaMetrics)
# exporters of the logs, jail logs, events, and nccl-profiles collectors.
# By default, chart values are used.
opentelemetry_batch = null

# Optional OpenTelemetry sending_queue overrides for logs, jail logs, events, and nccl-profiles collectors.
# By default, chart values are used.
opentelemetry_sending_queue = null

# Whether to delete jail stored logs after they have been read by the OpenTelemetry collector.
opentelemetry_delete_jail_logs_after_read = true

# Minimum time a log file (both shared and local) must remain unmodified before the OpenTelemetry collector
# deletes it after reading.
opentelemetry_delete_jail_logs_min_age = "4h"

# Whether to send system logs to Nebius o11y.
public_o11y_enabled = true

# Whether to move existing public o11y logs to the cluster's region.
allow_o11y_region_migration = false

# K8s version of the Mk8s cluster.
k8s_version = "1.36"

# Mk8s node infra version to pin in Mk8s nodegroups.
node_group_version = "75"

# Whether to add public IP address for each K8s node.
k8s_cluster_node_ssh_access_public_ip = false

# Lines to write to /etc/modprobe.d/nvidia_config.conf via cloud-init (GPU workers only).
nvidia_config_lines = [
  "options nvidia NVreg_RestrictProfilingToAdminUsers=0",
  "options nvidia NVreg_EnableStreamMemOPs=1",
  "options nvidia NVreg_RegistryDwords=\"PeerMappingOverride=1;\"",
]
