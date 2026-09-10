locals {
  kube_rbac_proxy = {
    image = "gcr.io/kubebuilder/kube-rbac-proxy"
    tag   = "v0.15.0"
  }
  helm = {
    repository = {
      slurm        = "oci://cr.eu-north1.nebius.cloud/soperator${!var.operator_stable ? "-unstable" : ""}"
      slurm_stable = "oci://cr.eu-north1.nebius.cloud/soperator"
      mariadb      = "https://helm.mariadb.com/mariadb-operator"
      raw          = "https://bedag.github.io/helm-charts/"
      spo          = "oci://cr.eu-north1.nebius.cloud/e00xdc03sb7gpqfd0a"
    }

    chart = {
      slurm_cluster         = "slurm-cluster"
      slurm_cluster_storage = "slurm-cluster-storage"
      slurm_operator_crds   = "soperator-crds"
      nodeconfigurator      = "nodeconfigurator"
      raw                   = "raw"
      spo                   = "security-profiles-operator"

      operator = {
        slurm       = "soperator"
        slurmchecks = "soperatorchecks"
        mariadb     = "mariadb-operator"
      }
    }

    version = {
      slurm   = var.operator_version
      mariadb = "25.10.2"
      raw     = "2.0.0"
      spo     = "0.8.4-soperator"
    }
  }

  image = {
    repository        = "cr.eu-north1.nebius.cloud/soperator${!var.operator_stable ? "-unstable" : ""}"
    repository_stable = "cr.eu-north1.nebius.cloud/soperator"
    tag               = var.operator_version
  }

  public_o11y_tsa_token_writer_source = "imds"

  gb300_enabled = anytrue([
    for nodeset in var.worker_nodesets : nodeset.gres_name == "nvidia_gb300"
  ])

  active_checks_on_worker_nodes = local.gb300_enabled

  soperator_active_checks_gpu_counts = distinct([for worker in var.node_capacity.worker : worker.gpus if worker.gpus > 0])
  // We don't support heterogenous clusters with mixed number of GPUs (or basically GB series mixed with the rest) yet.
  // So taking the first GPU count is fine for now.
  soperator_active_checks_gpus_per_node = length(local.soperator_active_checks_gpu_counts) == 1 ? tostring(local.soperator_active_checks_gpu_counts[0]) : null

  node_filters = {
    label = {
      gpu = module.labels.key_nvidia_gpu

      nodeset    = module.labels.key_slurm_nodeset_name
      system     = module.labels.name_nodeset_system
      controller = module.labels.name_nodeset_controller
      worker     = module.labels.name_nodeset_worker
      login      = module.labels.name_nodeset_login
      accounting = module.labels.name_nodeset_accounting
      nfs        = module.labels.name_nodeset_nfs
    }

    system = {
      name  = module.labels.name_nodeset_system
      match = module.labels.name_nodeset_system
    }
    controller = {
      name  = module.labels.name_nodeset_controller
      match = module.labels.name_nodeset_controller
    }
    worker = {
      name        = module.labels.name_nodeset_worker
      matches     = [module.labels.name_nodeset_worker]
      gpu_present = length([for i in range(length(var.node_count.worker)) : var.node_capacity.worker[i].gpus]) > 0
    }
    login = {
      name  = module.labels.name_nodeset_login
      match = module.labels.name_nodeset_login
    }
    accounting = {
      name  = module.labels.name_nodeset_accounting
      match = module.labels.name_nodeset_accounting
    }
    nfs = {
      name  = module.labels.name_nodeset_nfs
      match = module.labels.name_nodeset_nfs
    }
  }

  maintenance_ignore_node_labels = flatten([
    for group in var.maintenance_ignore_node_groups : [
      for match_value in try(
        [local.node_filters[group].match],
        try(local.node_filters[group].matches, [])
      ) :
      format("%s=%s", local.node_filters.label.nodeset, match_value)
    ]
  ])

  resources = {
    munge = {
      cpu               = 0.1
      memory            = 0.5
      ephemeral_storage = 5
    }
    sssd = {
      cpu               = 0.2
      memory            = 0.5
      ephemeral_storage = 5
    }
    # System/observability components are sized by the sizing tier,
    # with per-component overrides merged inside ../sizing_tier (see var.component_overrides).
    # local.selected_preset is the post-merge result.
    exporter                    = local.selected_preset.exporter
    rest                        = local.selected_preset.rest
    mariadb                     = local.selected_preset.mariadb
    node_configurator           = local.selected_preset.node_configurator
    soperator_main_controller   = local.selected_preset.soperator_main_controller
    soperator_checks_controller = local.selected_preset.soperator_checks_controller
    kruise_daemon               = local.selected_preset.kruise_daemon
    dcgm_exporter               = local.selected_preset.dcgm_exporter
    spo = {
      daemon     = local.selected_preset.spo_daemon
      controller = local.selected_preset.spo_controller
    }
    # The NFS server pod fills its dedicated node, so when an NFS nodeset exists
    # its node capacity (var.node_capacity.nfs) wins over the tier value.
    nfs_server = {
      limits = {
        memory = var.node_capacity.nfs != null ? var.node_capacity.nfs.memory_gibibytes : local.selected_preset.nfs_server.memory
      }
      requests = {
        memory = var.node_capacity.nfs != null ? var.node_capacity.nfs.memory_gibibytes : local.selected_preset.nfs_server.memory
        cpu    = var.node_capacity.nfs != null ? var.node_capacity.nfs.cpu_cores : local.selected_preset.nfs_server.cpu
      }
    }
  }

  slurm_node_extra = chomp(templatefile("${path.module}/templates/slurm_node_extra.tftpl", {
    rack_number           = null
    nvl_instance_group_id = ""
  }))

  slurm_node_extra_by_nodeset = {
    for nodeset in var.worker_nodesets : nodeset.name => chomp(templatefile("${path.module}/templates/slurm_node_extra.tftpl", {
      rack_number           = try(nodeset.rack_number, null)
      nvl_instance_group_id = try(trimspace(nodeset.nvl_instance_group_id), "")
    }))
  }

  # Calculate vmagent remote write queue count based on cluster size
  # This sets metrics ingestion capacity for larger clusters properly
  vm_agent_queue_count = 2 + floor(sum(var.node_count.worker) / 60)

  # Cap on the kube-state-metrics scrape response: an explicit var wins, otherwise the sizing
  # tier decides (null below M keeps vmagent's global 32MiB guard).
  kube_state_metrics_max_scrape_size = (
    var.kube_state_metrics_max_scrape_size != null
    ? var.kube_state_metrics_max_scrape_size
    : module.sizing.kube_state_metrics_max_scrape_size
  )

  # Total declared worker nodes across all worker nodesets. Drives the sizing tier.
  worker_count = length(var.node_count.worker) > 0 ? sum(var.node_count.worker) : 0

  # Sizing tier + effective per-component preset (tier column with var.component_overrides already merged).
  sizing_tier     = module.sizing.sizing_tier
  selected_preset = module.sizing.preset

  opentelemetry_batch_enabled = (
    var.opentelemetry_batch != null
    ? anytrue([
      var.opentelemetry_batch.timeout != null,
      var.opentelemetry_batch.send_batch_size != null,
      var.opentelemetry_batch.send_batch_max_size != null,
    ])
    : false
  )

  opentelemetry_sending_queue_enabled = (
    var.opentelemetry_sending_queue != null
    ? anytrue([
      var.opentelemetry_sending_queue.size != null,
      var.opentelemetry_sending_queue.num_consumers != null,
    ])
    : false
  )

  namespace = {
    logs       = "logs-system"
    monitoring = "monitoring-system"
  }

  metrics_collector = {
    host = "vmsingle-metrics-victoria-metrics-k8s-stack.${local.namespace.monitoring}.svc.cluster.local"
    port = 8429
  }
}
