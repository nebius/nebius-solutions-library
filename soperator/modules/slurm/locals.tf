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

  soperator_active_checks_gpu_counts = distinct([for worker in var.resources.worker : worker.gpus if worker.gpus > 0])
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
      gpu_present = length([for i in range(length(var.node_count.worker)) : var.resources.worker[i].gpus]) > 0
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
    exporter = {
      cpu               = 0.25
      memory            = 0.25
      ephemeral_storage = 0.5
    }
    rest = {
      cpu               = 2
      memory            = 8
      ephemeral_storage = 0.5
    }
    mariadb = {
      cpu               = 2
      memory            = 12
      ephemeral_storage = 16
    }
    node_configurator = {
      limits = {
        memory = 0.25
      }
      requests = {
        memory = 0.25
        cpu    = 0.5
      }
    }
    slurm_operator = {
      limits = {
        memory = 2
      }
      requests = {
        memory = 2
        cpu    = 1
      }
    }
    slurm_checks = {
      limits = {
        memory = 2
      }
      requests = {
        memory = 2
        cpu    = 0.5
      }
    }
    kruise_daemon = {
      cpu    = 0.05
      memory = 0.128
    }
    dcgm_exporter = {
      cpu    = 0.05
      memory = 0.5
    }
    nfs_server = {
      limits = {
        memory = var.resources.nfs != null ? var.resources.nfs.memory_gibibytes : 1
      }
      requests = {
        memory = var.resources.nfs != null ? var.resources.nfs.memory_gibibytes : 1
        cpu    = var.resources.nfs != null ? var.resources.nfs.cpu_cores : 1
      }
    }
  }

  slurm_node_extra = "\\\"{ \\\\\\\"ib_pod\\\\\\\": \\\\\\\"$TOPO_SWITCH_TIER2\\\\\\\", \\\\\\\"ib_su\\\\\\\": \\\\\\\"$TOPO_SWITCH_TIER1\\\\\\\" }\\\""

  # Calculate vmagent remote write queue count based on cluster size
  # This sets metrics ingestion capacity for larger clusters properly
  vm_agent_queue_count = 2 + floor(sum(var.node_count.worker) / 60)

  namespace = {
    logs       = "logs-system"
    monitoring = "monitoring-system"
  }

  metrics_collector = {
    host = "vmsingle-metrics-victoria-metrics-k8s-stack.${local.namespace.monitoring}.svc.cluster.local"
    port = 8429
  }
}
