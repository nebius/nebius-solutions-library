locals {
  resources = {
    system     = module.resources.by_platform[var.slurm_nodeset_system.resource.platform][var.slurm_nodeset_system.resource.preset]
    controller = module.resources.by_platform[var.slurm_nodeset_controller.resource.platform][var.slurm_nodeset_controller.resource.preset]
    workers    = [for worker in local.slurm_nodeset_workers : module.resources.by_platform[worker.resource.platform][worker.resource.preset]]
    login      = module.resources.by_platform[var.slurm_nodeset_login.resource.platform][var.slurm_nodeset_login.resource.preset]
    accounting = var.slurm_nodeset_accounting != null ? module.resources.by_platform[var.slurm_nodeset_accounting.resource.platform][var.slurm_nodeset_accounting.resource.preset] : null
    nfs        = var.slurm_nodeset_nfs != null ? module.resources.by_platform[var.slurm_nodeset_nfs.resource.platform][var.slurm_nodeset_nfs.resource.preset] : null
  }

  # keep in sync with helm chart
  # https://github.com/nebius/soperator/blob/main/helm/storageclasses/templates/storageclasses.yaml#L4
  storage_class_prefix = "compute-csi"

  slurm_cluster_name = "soperator"
  flux_namespace     = "flux-system"
  k8s_cluster_name   = format("soperator-%s", var.company_name)

  gb300_platform              = "gpu-gb300"
  gb300_nodes_per_nodegroup   = 18
  nvl_instance_group_size     = 18
  default_nodes_per_nodegroup = 100
  gb300_enabled               = anytrue([for nodeset in var.slurm_nodeset_workers : nodeset.resource.platform == local.gb300_platform])

  # GB300 keeps slurm_nodeset_login.size non-zero in tfvars so Soperator still
  # creates login pods, while Terraform skips the separate unused CPU login node
  # group. Non-GB300 platforms keep the configured login node group behavior.
  login_node_group = merge(var.slurm_nodeset_login, {
    node_group_enabled = local.gb300_enabled ? false : var.slurm_nodeset_login.node_group_enabled
  })

  # Normalize user-facing worker nodesets into the internal nodeset list used
  # by both mk8s node groups and Slurm NodeSets. GB300 is rack-addressed, so one
  # input nodeset expands into 18-node rack chunks named <name>-rack<rack>.
  # Example: { name = "worker", platform = "gpu-gb300", size = 36 } becomes
  # [{ name = "worker-rack0", size = 18 }, { name = "worker-rack1", size = 18 }].
  # Non-production partial racks keep their requested size, for example size = 10
  # becomes [{ name = "worker-rack0", size = 10 }]. Size = 0 keeps a
  # zero-replica rack nodeset so Terraform can downscale the generated node
  # groups while the Slurm NodeSet remains addressable.
  slurm_nodeset_workers = flatten([
    for nodeset in var.slurm_nodeset_workers :
    nodeset.resource.platform == local.gb300_platform ? [
      for rack in range(max(1, ceil(nodeset.size / local.gb300_nodes_per_nodegroup))) : merge(nodeset, {
        name = format(
          "%s-rack%d",
          nodeset.name,
          rack,
        )
        size                = min(local.gb300_nodes_per_nodegroup, nodeset.size - rack * local.gb300_nodes_per_nodegroup)
        nodes_per_nodegroup = local.gb300_nodes_per_nodegroup
      })
      ] : [merge(nodeset, {
        nodes_per_nodegroup = local.default_nodes_per_nodegroup
    })]
  ])

  backups_enabled = (var.backups_enabled == "force_enable" ||
  (var.backups_enabled == "auto" && local.filestore_jail_calculated_size_gibibytes < 12 * 1024))

  # Legacy node_group_workers for old-style deployments (without nodesets).
  # Splits each normalized nodeset into mk8s node group chunks.
  # Example: size = 250 and nodes_per_nodegroup = 100 produces sizes [100, 100, 50].
  node_group_workers = flatten([for i, nodeset in local.slurm_nodeset_workers : [
    for subset in range(ceil(nodeset.size / nodeset.nodes_per_nodegroup)) : {
      size                    = min(nodeset.nodes_per_nodegroup, nodeset.size - subset * nodeset.nodes_per_nodegroup)
      max_unavailable_percent = 50
      max_surge_percent       = null
      drain_timeout           = null
      resource                = nodeset.resource
      boot_disk               = nodeset.boot_disk
      gpu_cluster             = nodeset.gpu_cluster
      nodeset_index           = i
      subset_index            = subset
      preemptible             = nodeset.preemptible
    }
  ]])

  # V2 node_group_workers for new-style deployments (with nodesets)
  # Non-GB300 workers keep autoscaling and split into nodes_per_nodegroup chunks.
  # GB300 workers are fixed generated rack-sized groups because NVLink instance
  # groups are rack-scoped. Example: non-GB300 size = 128 becomes worker-0 size
  # 100 and worker-1 size 28; GB300 worker-rack0 stays size/min/max
  # equal to its normalized rack size with autoscaling off.
  node_group_workers_v2 = flatten([for i, nodeset in local.slurm_nodeset_workers : [
    for subset in range(ceil(nodeset.size / nodeset.nodes_per_nodegroup)) : {
      name            = nodeset.name
      node_group_name = nodeset.resource.platform == local.gb300_platform ? nodeset.name : join("-", [nodeset.name, subset])
      size            = nodeset.resource.platform == local.gb300_platform ? nodeset.size : min(nodeset.nodes_per_nodegroup, nodeset.size - subset * nodeset.nodes_per_nodegroup)
      min_size = nodeset.resource.platform == local.gb300_platform ? nodeset.size : (
        nodeset.autoscaling.enabled && nodeset.autoscaling.min_size != null
        # Fill autoscaling min_size left to right. Example: min_size = 120 over
        # 100-node chunks gives per-node-group min sizes [100, 20, 0].
        ? min(nodeset.nodes_per_nodegroup, max(0, nodeset.autoscaling.min_size - subset * nodeset.nodes_per_nodegroup))
        : min(nodeset.nodes_per_nodegroup, nodeset.size - subset * nodeset.nodes_per_nodegroup) # min=max
      )
      max_size               = nodeset.resource.platform == local.gb300_platform ? nodeset.size : max(1, min(nodeset.nodes_per_nodegroup, nodeset.size - subset * nodeset.nodes_per_nodegroup))
      autoscaling            = nodeset.resource.platform == local.gb300_platform ? false : nodeset.autoscaling.enabled
      resource               = nodeset.resource
      boot_disk              = nodeset.boot_disk
      gpu_cluster            = nodeset.gpu_cluster
      nodeset_index          = i
      subset_index           = subset
      preemptible            = nodeset.preemptible
      reservation_policy     = nodeset.reservation_policy
      nvlink                 = nodeset.nvlink
      placement_policy_nodes = nodeset.placement_policy_nodes
      local_nvme = {
        enabled         = try(nodeset.local_nvme.enabled, false)
        mount_path      = try(nodeset.local_nvme.mount_path, "/mnt/local-nvme")
        filesystem_type = try(nodeset.local_nvme.filesystem_type, "ext4")
      }
    }
  ]])

  # Key by final mk8s node group name so NVLink resources can be created and
  # looked up with the same identifier.
  node_group_workers_v2_by_key = {
    for worker in local.node_group_workers_v2 :
    worker.node_group_name => worker
  }
}

resource "terraform_data" "check_variables" {
  depends_on = [
    terraform_data.check_slurm_nodeset,
    terraform_data.check_slurm_nodeset_accounting,
    terraform_data.check_nfs,
    terraform_data.check_local_nvme,
  ]
}

module "filestore" {
  source = "../../modules/filestore"

  depends_on = [
    terraform_data.check_variables,
  ]

  iam_project_id = data.nebius_iam_v1_project.this.id

  k8s_cluster_name = local.k8s_cluster_name

  controller_spool = {
    spec = var.filestore_controller_spool.spec != null ? {
      disk_type            = "NETWORK_SSD"
      size_gibibytes       = var.filestore_controller_spool.spec.size_gibibytes
      block_size_kibibytes = var.filestore_controller_spool.spec.block_size_kibibytes
      forbid_deletion      = var.filestore_controller_spool.spec.forbid_deletion
    } : null
    existing = var.filestore_controller_spool.existing != null ? {
      id = var.filestore_controller_spool.existing.id
    } : null
  }

  accounting = var.accounting_enabled ? {
    spec = var.filestore_accounting.spec != null ? {
      disk_type            = "NETWORK_SSD"
      size_gibibytes       = var.filestore_accounting.spec.size_gibibytes
      block_size_kibibytes = var.filestore_accounting.spec.block_size_kibibytes
      forbid_deletion      = var.filestore_accounting.spec.forbid_deletion
    } : null
    existing = var.filestore_accounting.existing != null ? {
      id = var.filestore_accounting.existing.id
    } : null
  } : null

  jail = {
    spec = var.filestore_jail.spec != null ? {
      disk_type            = "NETWORK_SSD"
      size_gibibytes       = var.filestore_jail.spec.size_gibibytes
      block_size_kibibytes = var.filestore_jail.spec.block_size_kibibytes
      forbid_deletion      = var.filestore_jail.spec.forbid_deletion
    } : null
    existing = var.filestore_jail.existing != null ? {
      id = var.filestore_jail.existing.id
    } : null
  }

  jail_submounts = [for submount in var.filestore_jail_submounts : {
    name = submount.name
    spec = submount.spec != null ? {
      disk_type            = "NETWORK_SSD"
      size_gibibytes       = submount.spec.size_gibibytes
      block_size_kibibytes = submount.spec.block_size_kibibytes
      forbid_deletion      = submount.spec.forbid_deletion
    } : null
    existing = submount.existing != null ? {
      id = submount.existing.id
    } : null
  }]

  providers = {
    nebius = nebius
    units  = units
  }
}

module "nfs-server" {
  count = var.nfs.enabled ? 1 : 0

  source = "../../../modules/nfs-server"

  parent_id = data.nebius_iam_v1_project.this.id
  subnet_id = data.nebius_vpc_v1_subnet.this.id

  platform      = var.nfs.resource.platform
  preset        = var.nfs.resource.preset
  instance_name = "${local.k8s_cluster_name}-nfs-server"

  nfs_disk_name_suffix = local.k8s_cluster_name
  nfs_ip_range         = data.nebius_vpc_v1_subnet.this.status.ipv4_private_cidrs[0]
  nfs_size             = provider::units::from_gib(var.nfs.size_gibibytes)
  nfs_path             = "/nfs"

  ssh_user_name   = "soperator"
  ssh_public_keys = var.slurm_login_ssh_root_public_keys

  public_ip = var.nfs.public_ip

  providers = {
    nebius = nebius
  }
}

module "cleanup" {
  source = "../../modules/cleanup"

  iam_project_id = var.iam_project_id
}

module "k8s_cleanup" {
  source = "../../modules/k8s_cleanup"

  k8s_cluster_context = module.k8s.cluster_context
  k8s_cluster_id      = module.k8s.cluster_id

  depends_on = [
    module.k8s,
  ]
}

resource "nebius_compute_v1_nvl_instance_group" "worker" {
  for_each = {
    for key, worker in local.node_group_workers_v2_by_key :
    key => worker
    if try(worker.nvlink.enabled == true, false)
  }

  parent_id = var.iam_project_id
  size      = local.nvl_instance_group_size
  name      = "${local.k8s_cluster_name}-${each.value.node_group_name}"
  type      = each.value.nvlink.type
}

module "k8s" {
  depends_on = [
    module.filestore,
    module.nfs-server,
    module.cleanup,
    terraform_data.check_slurm_nodeset_accounting,
    terraform_data.check_slurm_nodeset,
  ]

  source = "../../modules/k8s"

  iam_project_id  = data.nebius_iam_v1_project.this.id
  vpc_subnet_id   = data.nebius_vpc_v1_subnet.this.id
  login_public_ip = var.slurm_login_public_ip

  k8s_version                  = var.k8s_version
  name                         = local.k8s_cluster_name
  company_name                 = var.company_name
  platform_driver_presets      = var.platform_driver_presets
  use_preinstalled_gpu_drivers = var.use_preinstalled_gpu_drivers

  etcd_cluster_size = var.etcd_cluster_size

  node_group_system     = var.slurm_nodeset_system
  node_group_controller = var.slurm_nodeset_controller
  node_group_workers    = local.node_group_workers
  node_group_workers_v2 = [
    for worker in local.node_group_workers_v2 : merge(worker, {
      nvl_instance_group_id = try(nebius_compute_v1_nvl_instance_group.worker[worker.node_group_name].id, "")
    })
  ]
  node_group_login = local.login_node_group
  node_group_accounting = {
    enabled = var.accounting_enabled
    spec    = var.slurm_nodeset_accounting
  }
  node_group_nfs = {
    enabled = var.slurm_nodeset_nfs != null
    spec    = var.slurm_nodeset_nfs
  }

  filestores = {
    controller_spool = {
      id        = module.filestore.controller_spool.id
      mount_tag = module.filestore.controller_spool.mount_tag
    }
    jail = {
      id        = module.filestore.jail.id
      mount_tag = module.filestore.jail.mount_tag
    }
    jail_submounts = [for key, submount in module.filestore.jail_submounts : {
      id        = submount.id
      mount_tag = submount.mount_tag
    }]
    accounting = var.accounting_enabled ? {
      id        = module.filestore.accounting.id
      mount_tag = module.filestore.accounting.mount_tag
    } : null
  }

  node_ssh_access_users = var.k8s_cluster_node_ssh_access_users
  nvidia_config_lines   = var.nvidia_config_lines

  providers = {
    nebius = nebius
    units  = units
  }
}

module "nvidia_operator_network" {
  count = module.k8s.gpu_involved && !var.use_preinstalled_gpu_drivers ? 1 : 0

  depends_on = [
    module.k8s
  ]

  source = "../../../modules/network-operator"

  cluster_id = module.k8s.cluster_id
  parent_id  = data.nebius_iam_v1_project.this.id

  providers = {
    nebius = nebius
  }
}

module "nvidia_operator_gpu" {
  count = module.k8s.gpu_involved && !var.use_preinstalled_gpu_drivers ? 1 : 0

  depends_on = [
    module.nvidia_operator_network
  ]

  source = "../../../modules/gpu-operator"

  cluster_id = module.k8s.cluster_id
  parent_id  = data.nebius_iam_v1_project.this.id

  enable_dcgm_exporter        = var.dcgm_job_mapping_enabled == false && var.telemetry_enabled
  enable_dcgm_service_monitor = var.dcgm_job_mapping_enabled == false && var.telemetry_enabled
  relabel_dcgm_exporter       = var.telemetry_enabled

  providers = {
    nebius = nebius
  }
}

module "o11y" {
  count = var.public_o11y_enabled ? 1 : 0

  depends_on = [
    module.k8s,
    module.fluxcd,
  ]

  source = "../../modules/o11y"

  iam_project_id      = var.iam_project_id
  o11y_iam_tenant_id  = var.o11y_iam_tenant_id
  o11y_profile        = var.o11y_profile
  k8s_cluster_context = module.k8s.cluster_context
  company_name        = var.company_name
}

module "slurm" {
  depends_on = [
    module.k8s,
    module.o11y,
    module.fluxcd,
    module.backups,
    # Forces destroy order slurm -> backups_store.cleanup_bucket -> bucket, so
    # Slurm backup workloads stop writing before the bucket is emptied. Without
    # this the cleanup ran in parallel with Slurm teardown and restic kept
    # writing to the bucket during aws s3 rm, causing BucketNotEmpty. See
    # SCHED-1401.
    module.backups_store,
  ]

  source = "../../modules/slurm"

  active_checks_scope = var.active_checks_scope

  region              = var.region
  iam_tenant_id       = var.iam_tenant_id
  iam_project_id      = var.iam_project_id
  cluster_name        = var.company_name
  name                = local.slurm_cluster_name
  k8s_cluster_context = module.k8s.cluster_context
  k8s_cluster_id      = module.k8s.cluster_id

  operator_version = var.slurm_operator_version
  operator_stable  = var.slurm_operator_stable

  maintenance                    = var.maintenance
  maintenance_ignore_node_groups = var.maintenance_ignore_node_groups

  use_preinstalled_gpu_drivers  = var.use_preinstalled_gpu_drivers
  cuda_version                  = lookup(var.platform_cuda_versions, local.slurm_nodeset_workers[0].resource.platform)
  controller_state_on_filestore = var.controller_state_on_filestore

  node_count = {
    controller = var.slurm_nodeset_controller.size
    worker     = [for workers in local.slurm_nodeset_workers : workers.size]
    login      = var.slurm_nodeset_login.size
  }

  resources = {
    system = {
      cpu_cores        = local.resources.system.cpu_cores
      memory_gibibytes = local.resources.system.memory_gibibytes
      ephemeral_storage_gibibytes = floor(
        var.slurm_nodeset_system.boot_disk.size_gibibytes * module.resources.k8s_ephemeral_storage_coefficient
        -module.resources.k8s_ephemeral_storage_reserve.gibibytes
      )
    }
    controller = {
      cpu_cores        = local.resources.controller.cpu_cores
      memory_gibibytes = floor(local.resources.controller.memory_gibibytes)
      ephemeral_storage_gibibytes = floor(
        var.slurm_nodeset_controller.boot_disk.size_gibibytes * module.resources.k8s_ephemeral_storage_coefficient
        -module.resources.k8s_ephemeral_storage_reserve.gibibytes
      )
    }
    worker = [for i, worker in local.slurm_nodeset_workers :
      {
        cpu_cores = local.resources.workers[i].cpu_cores - (
          worker.resource.platform == local.gb300_platform ? var.gb300_login_pod_worker_reserve.cpu_cores : 0
        )
        memory_gibibytes = floor(local.resources.workers[i].memory_gibibytes) - (
          worker.resource.platform == local.gb300_platform ? var.gb300_login_pod_worker_reserve.memory_gibibytes : 0
        )
        ephemeral_storage_gibibytes = floor(
          worker.boot_disk.size_gibibytes * module.resources.k8s_ephemeral_storage_coefficient
          -module.resources.k8s_ephemeral_storage_reserve.gibibytes
          -(worker.resource.platform == local.gb300_platform ? var.gb300_login_pod_worker_reserve.ephemeral_storage_gibibytes : 0)
        )
        gpus = local.resources.workers[i].gpus
      }
    ]
    login = local.gb300_enabled ? var.gb300_login_pod_worker_reserve : {
      cpu_cores        = local.resources.login.cpu_cores
      memory_gibibytes = floor(local.resources.login.memory_gibibytes)
      ephemeral_storage_gibibytes = floor(
        var.slurm_nodeset_login.boot_disk.size_gibibytes * module.resources.k8s_ephemeral_storage_coefficient
        -module.resources.k8s_ephemeral_storage_reserve.gibibytes
      )
    }
    accounting = var.accounting_enabled ? {
      cpu_cores        = local.resources.accounting.cpu_cores
      memory_gibibytes = floor(local.resources.accounting.memory_gibibytes)
      ephemeral_storage_gibibytes = floor(
        var.slurm_nodeset_accounting.boot_disk.size_gibibytes * module.resources.k8s_ephemeral_storage_coefficient
        -module.resources.k8s_ephemeral_storage_reserve.gibibytes
      )
    } : null
    nfs = var.slurm_nodeset_nfs != null ? {
      cpu_cores        = local.resources.nfs.cpu_cores
      memory_gibibytes = floor(local.resources.nfs.memory_gibibytes)
    } : null
  }

  filestores = {
    controller_spool = {
      size_gibibytes = module.filestore.controller_spool.size_gibibytes
      device         = module.filestore.controller_spool.mount_tag
    }
    jail = {
      size_gibibytes = module.filestore.jail.size_gibibytes
      device         = module.filestore.jail.mount_tag
    }
    jail_submounts = [for submount in var.filestore_jail_submounts : {
      name           = submount.name
      size_gibibytes = module.filestore.jail_submounts[submount.name].size_gibibytes
      device         = module.filestore.jail_submounts[submount.name].mount_tag
      mount_path     = submount.mount_path
    }]
    accounting = var.accounting_enabled ? {
      size_gibibytes = module.filestore.accounting.size_gibibytes
      device         = module.filestore.accounting.mount_tag
    } : null
  }
  nfs = {
    enabled    = var.nfs.enabled
    path       = var.nfs.enabled ? module.nfs-server[0].nfs_export_path : null
    host       = var.nfs.enabled ? module.nfs-server[0].nfs_server_internal_ip : null
    mount_path = var.nfs.enabled ? var.nfs.mount_path : null
  }

  nfs_in_k8s = {
    enabled        = var.nfs_in_k8s.enabled
    version        = var.nfs_in_k8s.version
    size_gibibytes = var.nfs_in_k8s.size_gibibytes
    storage_class  = replace("compute-csi-${lower(var.nfs_in_k8s.disk_type)}-${lower(var.nfs_in_k8s.filesystem_type)}", "_", "-")
    threads        = var.nfs_in_k8s.threads
  }
  nfs_node_group_enabled = var.slurm_nodeset_nfs != null

  exporter_enabled    = var.slurm_exporter_enabled
  rest_enabled        = var.slurm_rest_enabled
  accounting_enabled  = var.accounting_enabled
  telemetry_enabled   = var.telemetry_enabled
  public_o11y_enabled = var.public_o11y_enabled
  soperator_notifier  = var.soperator_notifier

  backups_enabled = local.backups_enabled
  backups_config = {
    secret_name    = local.backups_enabled ? module.backups[0].secret_name : null
    password       = var.backups_password
    schedule       = var.backups_schedule
    prune_schedule = var.backups_prune_schedule
    retention      = var.backups_retention
    storage = {
      bucket : local.backups_enabled ? module.backups_store[0].name : null,
      endpoint : local.backups_enabled ? module.backups_store[0].endpoint : null,
      bucket_id : local.backups_enabled ? module.backups_store[0].bucket_id : null
    }
  }

  slurmdbd_config         = var.slurmdbd_config
  slurm_accounting_config = var.slurm_accounting_config

  use_default_apparmor_profile    = var.use_default_apparmor_profile
  worker_sshd_config_map_ref_name = var.slurm_worker_sshd_config_map_ref_name
  login_on_worker_nodes           = local.gb300_enabled
  shared_memory_size_gibibytes    = var.slurm_shared_memory_size_gibibytes
  slurm_partition_config_type     = var.slurm_partition_config_type
  slurm_partition_raw_config      = var.slurm_partition_raw_config
  slurm_health_check_config       = var.slurm_health_check_config

  slurm_nodesets_partitions = [for partition in var.slurm_nodesets_partitions : {
    name         = partition.name
    is_all       = partition.is_all
    nodeset_refs = partition.slurm_nodeset_refs
    config       = partition.config
  }]
  worker_nodesets = [for nodeset in local.slurm_nodeset_workers : {
    name            = nodeset.name
    replicas        = nodeset.size
    max_unavailable = "20%"
    features = concat(
      [
        provider::string-functions::snake_case(nodeset.resource.platform),
        provider::string-functions::snake_case(nodeset.boot_disk.type),
      ],
      nodeset.features != null ? nodeset.features : []
    )
    cpu_topology                             = module.resources.cpu_topology_by_platform[nodeset.resource.platform][nodeset.resource.preset]
    gres_name                                = lookup(module.resources.gres_name_by_platform, nodeset.resource.platform, null)
    gres_config                              = lookup(module.resources.gres_config_by_platform, nodeset.resource.platform, null)
    create_partition                         = nodeset.create_partition != null ? nodeset.create_partition : false
    ephemeral_nodes                          = nodeset.ephemeral_nodes
    persistent_volume_claim_retention_policy = nodeset.persistent_volume_claim_retention_policy
    initial_number_ephemeral_nodes           = nodeset.initial_number_ephemeral_nodes
    local_nvme = {
      enabled         = try(nodeset.local_nvme.enabled, false)
      mount_path      = try(nodeset.local_nvme.mount_path, "/mnt/local-nvme")
      filesystem_type = try(nodeset.local_nvme.filesystem_type, "ext4")
    }
    node_local_jail_submounts = [for sm in nodeset.node_local_jail_submounts : {
      name               = sm.name
      mount_path         = sm.mount_path
      size_gibibytes     = sm.size_gibibytes
      disk_type          = sm.disk_type
      filesystem_type    = sm.filesystem_type
      storage_class_name = replace("${local.storage_class_prefix}-${lower(sm.disk_type)}-${lower(sm.filesystem_type)}", "_", "-")
    }]
    node_local_image_storage = {
      enabled = nodeset.node_local_image_disk.enabled
      spec = nodeset.node_local_image_disk.enabled ? {
        size_gibibytes     = nodeset.node_local_image_disk.spec.size_gibibytes
        filesystem_type    = nodeset.node_local_image_disk.spec.filesystem_type
        storage_class_name = replace("${local.storage_class_prefix}-${lower(nodeset.node_local_image_disk.spec.disk_type)}-${lower(nodeset.node_local_image_disk.spec.filesystem_type)}", "_", "-")
      } : null
    }
  }]

  topology = {
    plugin     = local.gb300_enabled ? "topology/block" : "topology/tree"
    block_size = local.gb300_enabled ? try(var.slurm_topology_block_size, local.gb300_nodes_per_nodegroup) : null
  }

  login_allocation_id              = module.k8s.static_ip_allocation_id
  login_public_ip                  = var.slurm_login_public_ip
  tailscale_enabled                = var.tailscale_enabled
  login_sshd_config_map_ref_name   = var.slurm_login_sshd_config_map_ref_name
  sssd_conf_secret_ref_name        = var.slurm_sssd_conf_secret_ref_name
  sssd_ldap_ca_config_map_ref_name = var.slurm_sssd_ldap_ca_config_map_ref_name
  sssd_enabled                     = var.slurm_sssd_enabled
  login_ssh_root_public_keys       = var.slurm_login_ssh_root_public_keys

  flux_namespace = local.flux_namespace

  providers = {
    helm = helm
  }
}

module "login_script" {
  depends_on = [
    module.slurm
  ]

  source = "../../modules/login"

  slurm_cluster_name = local.slurm_cluster_name

  k8s_cluster_context = module.k8s.cluster_context

  providers = {
    kubernetes = kubernetes
  }
}

module "backups_store" {
  count = local.backups_enabled ? 1 : 0

  source = "../../modules/backups_store"

  iam_project_id = var.iam_project_id
  instance_name  = local.k8s_cluster_name

  cleanup_bucket_on_destroy = var.cleanup_bucket_on_destroy

  depends_on = [
    module.k8s,
    module.fluxcd,
  ]
}

module "backups" {
  count = local.backups_enabled ? 1 : 0

  source = "../../modules/backups"

  k8s_cluster_context = module.k8s.cluster_context
  k8s_cluster_id      = module.k8s.cluster_id

  iam_project_id      = var.iam_project_id
  iam_tenant_id       = var.iam_tenant_id
  instance_name       = local.k8s_cluster_name
  soperator_namespace = local.slurm_cluster_name
  backups_password    = var.backups_password

  providers = {
    nebius = nebius
  }

  depends_on = [
    module.k8s,
  ]
}

module "fluxcd" {
  depends_on = [
    module.k8s,
  ]
  source              = "../../modules/fluxcd"
  k8s_cluster_context = module.k8s.cluster_context
}
