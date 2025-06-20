locals {
  resources = {
    system     = module.resources.by_platform[var.slurm_nodeset_system.resource.platform][var.slurm_nodeset_system.resource.preset]
    controller = module.resources.by_platform[var.slurm_nodeset_controller.resource.platform][var.slurm_nodeset_controller.resource.preset]
    workers    = [for worker in var.slurm_nodeset_workers : module.resources.by_platform[worker.resource.platform][worker.resource.preset]]
    login      = module.resources.by_platform[var.slurm_nodeset_login.resource.platform][var.slurm_nodeset_login.resource.preset]
    accounting = var.slurm_nodeset_accounting != null ? module.resources.by_platform[var.slurm_nodeset_accounting.resource.platform][var.slurm_nodeset_accounting.resource.preset] : null
  }

  slurm_cluster_name = "soperator"
  flux_namespace     = "flux-system"
  k8s_cluster_name   = format("soperator-%s", var.company_name)

  backups_enabled = (var.backups_enabled == "force_enable" ||
  (var.backups_enabled == "auto" && local.filestore_jail_calculated_size_gibibytes < 12 * 1024))
}

resource "terraform_data" "check_variables" {
  depends_on = [
    terraform_data.check_slurm_nodeset,
    terraform_data.check_slurm_nodeset_accounting,
    terraform_data.check_nfs,
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
  nfs_path             = "/home"

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

module "k8s" {
  depends_on = [
    module.filestore,
    module.nfs-server,
    module.cleanup,
    terraform_data.check_slurm_nodeset_accounting,
    terraform_data.check_slurm_nodeset,
  ]

  source = "../../modules/k8s"

  iam_project_id = data.nebius_iam_v1_project.this.id
  vpc_subnet_id  = data.nebius_vpc_v1_subnet.this.id

  k8s_version  = var.k8s_version
  name         = local.k8s_cluster_name
  company_name = var.company_name

  etcd_cluster_size = var.etcd_cluster_size

  node_group_system     = var.slurm_nodeset_system
  node_group_controller = var.slurm_nodeset_controller
  node_group_workers = flatten([for i, nodeset in var.slurm_nodeset_workers :
    [
      for subset in range(ceil(nodeset.size / nodeset.nodes_per_nodegroup)) :
      {
        size                    = nodeset.nodes_per_nodegroup
        max_unavailable_percent = nodeset.max_unavailable_percent
        resource                = nodeset.resource
        boot_disk               = nodeset.boot_disk
        gpu_cluster             = nodeset.gpu_cluster
        nodeset_index           = i
        subset_index            = subset
      }
    ]
  ])
  node_group_login = var.slurm_nodeset_login
  node_group_accounting = {
    enabled = var.accounting_enabled
    spec    = var.slurm_nodeset_accounting
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

  providers = {
    nebius = nebius
    units  = units
  }
}

module "k8s_storage_class" {
  count = (
    (
      length(var.node_local_jail_submounts) > 0 ||
      var.node_local_image_disk.enabled
    )
    ? 1
    : 0
  )

  depends_on = [
    module.k8s,
  ]

  source = "../../modules/k8s/storage_class"

  storage_class_requirements = concat([for sm in var.node_local_jail_submounts : {
    disk_type       = sm.disk_type
    filesystem_type = sm.filesystem_type
    }], !var.node_local_image_disk.enabled ? [] : [{
    disk_type       = var.node_local_image_disk.spec.disk_type
    filesystem_type = var.node_local_image_disk.spec.filesystem_type
  }])

  providers = {
    kubernetes = kubernetes
  }
}

module "nvidia_operator_network" {
  count = module.k8s.gpu_involved ? 1 : 0

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
  count = module.k8s.gpu_involved ? 1 : 0

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
  ]

  source = "../../modules/o11y"

  o11y_iam_group_id   = var.o11y_iam_group_id
  o11y_iam_project_id = var.o11y_iam_project_id
  o11y_iam_tenant_id  = var.o11y_iam_tenant_id
  o11y_profile        = var.o11y_profile
  k8s_cluster_context = module.k8s.cluster_context
  region              = var.region
  company_name        = var.company_name
}

module "slurm" {
  depends_on = [
    module.k8s,
    module.k8s_storage_class,
    module.o11y,
    module.fluxcd,
    module.backups,
  ]

  source = "../../modules/slurm"

  name                         = local.slurm_cluster_name
  cluster_name                 = var.company_name
  operator_version             = var.slurm_operator_version
  operator_stable              = var.slurm_operator_stable
  k8s_cluster_context          = module.k8s.cluster_context
  maintenance                  = var.maintenance
  use_default_apparmor_profile = var.use_default_apparmor_profile
  public_o11y_enabled          = var.public_o11y_enabled
  slurm_partition_config_type  = var.slurm_partition_config_type
  slurm_partition_raw_config   = var.slurm_partition_raw_config
  slurm_worker_features        = var.slurm_worker_features
  slurm_health_check_config    = var.slurm_health_check_config
  flux_namespace               = local.flux_namespace
  backups_enabled              = local.backups_enabled

  github_org              = var.github_org
  github_repository       = var.github_repository
  github_branch           = var.slurm_operator_stable ? "main" : "dev"
  flux_interval           = var.flux_interval
  flux_kustomization_path = var.slurm_operator_stable ? "fluxcd/environment/nebius-cloud/prod" : "fluxcd/environment/nebius-cloud/dev"

  iam_project_id = var.iam_project_id

  node_count = {
    controller = var.slurm_nodeset_controller.size
    worker     = [for workers in var.slurm_nodeset_workers : workers.size]
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
    worker = [for i, worker in var.slurm_nodeset_workers :
      {
        cpu_cores        = local.resources.workers[i].cpu_cores
        memory_gibibytes = floor(local.resources.workers[i].memory_gibibytes)
        ephemeral_storage_gibibytes = floor(
          worker.boot_disk.size_gibibytes * module.resources.k8s_ephemeral_storage_coefficient
          -module.resources.k8s_ephemeral_storage_reserve.gibibytes
        )
        gpus = local.resources.workers[i].gpus
      }
    ]
    login = {
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
  }

  login_allocation_id            = module.k8s.static_ip_allocation_id
  login_sshd_config_map_ref_name = var.slurm_login_sshd_config_map_ref_name
  login_ssh_root_public_keys     = var.slurm_login_ssh_root_public_keys

  worker_sshd_config_map_ref_name = var.slurm_worker_sshd_config_map_ref_name

  exporter_enabled        = var.slurm_exporter_enabled
  rest_enabled            = var.slurm_rest_enabled
  accounting_enabled      = var.accounting_enabled
  slurmdbd_config         = var.slurmdbd_config
  slurm_accounting_config = var.slurm_accounting_config

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
  node_local_jail_submounts = [for sm in var.node_local_jail_submounts : {
    name               = sm.name
    mount_path         = sm.mount_path
    size_gibibytes     = sm.size_gibibytes
    disk_type          = sm.disk_type
    filesystem_type    = sm.filesystem_type
    storage_class_name = one(module.k8s_storage_class).storage_classes[sm.disk_type][sm.filesystem_type]
  }]
  node_local_image_storage = {
    enabled = var.node_local_image_disk.enabled
    spec = {
      size_gibibytes     = var.node_local_image_disk.spec.size_gibibytes
      filesystem_type    = var.node_local_image_disk.spec.filesystem_type
      storage_class_name = one(module.k8s_storage_class).storage_classes[var.node_local_image_disk.spec.disk_type][var.node_local_image_disk.spec.filesystem_type]
    }
  }

  nfs = {
    enabled    = var.nfs.enabled
    path       = var.nfs.enabled ? module.nfs-server[0].nfs_export_path : null
    host       = var.nfs.enabled ? module.nfs-server[0].nfs_server_internal_ip : null
    mount_path = var.nfs.enabled ? var.nfs.mount_path : null
  }

  shared_memory_size_gibibytes = var.slurm_shared_memory_size_gibibytes

  default_prolog_enabled = var.default_prolog_enabled
  default_epilog_enabled = var.default_epilog_enabled

  nccl_topology_type           = "auto"
  nccl_benchmark_enable        = var.nccl_benchmark_enable
  nccl_benchmark_schedule      = var.nccl_benchmark_schedule
  nccl_benchmark_min_threshold = var.nccl_benchmark_min_threshold
  nccl_use_infiniband          = var.nccl_use_infiniband

  telemetry_enabled = var.telemetry_enabled

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

  depends_on = [
    module.k8s,
    module.fluxcd,
  ]
}

module "backups" {
  count = local.backups_enabled ? 1 : 0

  source = "../../modules/backups"

  k8s_cluster_context = module.k8s.cluster_context

  iam_project_id      = var.iam_project_id
  iam_tenant_id       = var.iam_tenant_id
  instance_name       = local.k8s_cluster_name
  flux_namespace      = local.flux_namespace
  soperator_namespace = local.slurm_cluster_name
  bucket_name         = module.backups_store[count.index].name
  bucket_endpoint     = module.backups_store[count.index].endpoint

  backups_password  = var.backups_password
  backups_schedule  = var.backups_schedule
  prune_schedule    = var.backups_prune_schedule
  backups_retention = var.backups_retention

  providers = {
    nebius = nebius
    helm   = helm
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

module "active_checks" {
  source = "../../modules/active_checks"

  k8s_cluster_context     = module.k8s.cluster_context
  slurm_cluster_name      = local.slurm_cluster_name
  slurm_cluster_namespace = local.slurm_cluster_name
  num_of_login_nodes      = var.slurm_nodeset_login.size

  depends_on = [
    module.slurm
  ]
}
