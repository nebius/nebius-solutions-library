resource "terraform_data" "wait_for_slurm_cluster_hr" {
  depends_on = [
    helm_release.soperator_fluxcd_bootstrap,
  ]

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command = templatefile("${path.module}/scripts/wait_for_flux_hr.sh.tmpl", {
      k8s_cluster_context = var.k8s_cluster_context
      helmrelease_name    = "flux-system-soperator-fluxcd-slurm-cluster"
      timeout_minutes     = 60
    })
  }
}

resource "terraform_data" "wait_for_soperator_activechecks_hr" {
  depends_on = [
    helm_release.soperator_fluxcd_bootstrap,
    terraform_data.wait_for_slurm_cluster_hr,
  ]

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command = templatefile("${path.module}/scripts/wait_for_flux_hr.sh.tmpl", {
      k8s_cluster_context = var.k8s_cluster_context
      helmrelease_name    = "flux-system-soperator-fluxcd-soperator-activechecks"
      timeout_minutes     = 240
    })
  }
}

resource "terraform_data" "wait_for_slurm_cluster_available" {
  depends_on = [
    terraform_data.wait_for_slurm_cluster_hr
  ]

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command = join(
      " ",
      [
        "kubectl", "wait",
        "--for=jsonpath='{.status.phase}'=Available",
        "--timeout", "1h",
        "--context", var.k8s_cluster_context,
        "-n", var.name,
        "slurmcluster.slurm.nebius.ai/${var.name}"
      ]
    )
  }
}

resource "helm_release" "soperator_fluxcd_cm" {
  name       = "terraform-fluxcd-values"
  repository = local.helm.repository.raw
  chart      = local.helm.chart.raw
  version    = local.helm.version.raw
  namespace  = var.flux_namespace

  values = [templatefile("${path.module}/templates/helm_values/terraform_fluxcd_values.yaml.tftpl", {
    soperator_active_checks_override_block    = indent(14, local.soperator_activechecks_override_yaml)
    soperator_active_checks_on_worker_nodes   = local.active_checks_on_worker_nodes
    soperator_active_checks_gpus_per_node     = local.soperator_active_checks_gpus_per_node
    soperator_checks_extensive_check_enabled  = !local.gb300_enabled
    soperator_checks_node_replacement_enabled = !local.gb300_enabled

    telemetry_enabled  = var.telemetry_enabled
    accounting_enabled = var.accounting_enabled
    iam_tenant_id      = var.iam_tenant_id
    iam_project_id     = var.iam_project_id
    k8s_cluster_id     = var.k8s_cluster_id

    backups_enabled = var.backups_enabled
    backups_config  = var.backups_enabled ? var.backups_config : null

    soperator_helm_repo      = local.helm.repository.slurm
    soperator_helm_repo_nfs  = var.nfs_in_k8s.use_stable_repo ? local.helm.repository.slurm_stable : local.helm.repository.slurm
    soperator_image_repo     = local.image.repository
    soperator_image_repo_nfs = var.nfs_in_k8s.use_stable_repo ? local.image.repository_stable : local.image.repository

    dcgm_exporter_enabled          = var.dcgm_exporter_enabled
    enroot_direct_squashfs_enabled = var.enroot_direct_squashfs_enabled
    # Cluster-wide toggle for the [program:dockerd] block in the shared jail
    # supervisord config (customConfigmaps), which is one configmap for the whole
    # cluster and cannot be made per-nodeset. It is NOT the per-nodeset Docker
    # switch: each nodeset's Docker is gated separately by its own
    # node_local_image_storage.enabled in the nodesets values (docker-proxy sidecar
    # + masking docker/dockerd binaries in the jail). Here we only decide whether to
    # keep dockerd in the shared supervisord at all: keep it if any nodeset has
    # image-storage disks (Docker requires them for OCI data), drop it entirely only
    # when no nodeset does, so disk-less clusters don't restart-loop a stub dockerd.
    docker_enabled = anytrue([for nodeset in var.worker_nodesets : nodeset.node_local_image_storage.enabled])

    tailscale_enabled       = var.tailscale_enabled
    apparmor_enabled        = var.use_default_apparmor_profile
    enable_soperator_checks = var.enable_soperator_checks

    operator_version                          = var.operator_version
    cert_manager_version                      = var.cert_manager_version
    k8up_version                              = var.k8up_version
    mariadb_operator_version                  = var.mariadb_operator_version
    opentelemetry_collector_version           = var.opentelemetry_collector_version
    opentelemetry_batch                       = var.opentelemetry_batch
    opentelemetry_batch_enabled               = local.opentelemetry_batch_enabled
    opentelemetry_sending_queue               = var.opentelemetry_sending_queue
    opentelemetry_sending_queue_enabled       = local.opentelemetry_sending_queue_enabled
    opentelemetry_delete_jail_logs_after_read = var.opentelemetry_delete_jail_logs_after_read
    opentelemetry_delete_jail_logs_min_age    = var.opentelemetry_delete_jail_logs_min_age
    prometheus_crds_version                   = var.prometheus_crds_version
    security_profiles_operator_version        = var.security_profiles_operator_version
    vmstack_version                           = var.vmstack_version
    vmstack_crds_version                      = var.vmstack_crds_version
    vmlogs_version                            = var.vmlogs_version
    notifier                                  = var.soperator_notifier
    nccl_inspector_profiling                  = var.nccl_inspector_profiling

    name                    = var.name
    cluster_name            = var.cluster_name
    region                  = var.region
    public_o11y_enabled     = var.public_o11y_enabled
    tsa_token_writer_source = local.public_o11y_tsa_token_writer_source
    has_local_nvme          = anytrue([for nodeset in var.worker_nodesets : try(nodeset.local_nvme.enabled, false)])
    has_nccl_network_vars   = length(local.worker_nccl_network_vars) > 0
    metrics_collector       = local.metrics_collector
    create_pvcs             = var.create_pvcs

    slurm_cluster_storage = {
      scheduling = local.node_filters

      volume = {
        controller_spool = {
          size   = "${var.filestores.controller_spool.size_gibibytes}Gi"
          device = var.filestores.controller_spool.device
        }
        jail = {
          size   = "${var.filestores.jail.size_gibibytes}Gi"
          device = var.filestores.jail.device
        }
        jail_submounts = [for submount in var.filestores.jail_submounts : {
          name   = submount.name
          size   = "${submount.size_gibibytes}Gi"
          device = submount.device
        }]
        accounting = var.accounting_enabled ? {
          enabled = true
          size    = "${var.filestores.accounting.size_gibibytes}Gi"
          device  = var.filestores.accounting.device
        } : { enabled = false }
      }
    }

    slurm_cluster = {
      maintenance = var.maintenance

      partition_configuration = {
        slurm_config_type = var.slurm_partition_config_type
        slurm_raw_config  = var.slurm_partition_raw_config
      }

      topology = {
        topologies = var.topology.topologies
      }

      use_preinstalled_gpu_drivers = var.use_preinstalled_gpu_drivers
      cuda_version                 = var.cuda_version

      slurm_health_check_config = var.slurm_health_check_config

      k8s_node_filters               = local.node_filters
      maintenance_ignore_node_labels = local.maintenance_ignore_node_labels

      # GB300 worker nodes are ARM. The populate-jail job must run on an ARM
      # node so Kubernetes pulls the ARM image variant and writes ARM binaries
      # into the jail during first cluster creation. Other platforms keep the
      # historical system-node placement.
      populate_jail = {
        k8s_node_filter_name = var.login_on_worker_nodes ? local.node_filters.worker.name : local.node_filters.system.name
      }

      jail_submounts = [for submount in var.filestores.jail_submounts : {
        name       = submount.name
        mount_path = submount.mount_path
      }]

      controller_state_on_filestore = var.controller_state_on_filestore

      nfs                    = var.nfs
      nfs_in_k8s             = var.nfs_in_k8s
      nfs_node_group_enabled = var.nfs_node_group_enabled

      nodes = {
        accounting = {
          enabled              = var.accounting_enabled
          use_protected_secret = var.use_protected_secret
          mariadb_operator = var.accounting_enabled ? {
            enabled         = var.accounting_enabled
            storage_size    = var.accounting_enabled ? var.filestores.accounting.size_gibibytes : 0
            metrics_enabled = var.telemetry_enabled
            resources       = local.resources.mariadb
          } : null
          slurmdbd_config = var.slurmdbd_config
          slurm_config    = var.slurm_accounting_config
          resources = var.accounting_enabled ? {
            cpu               = var.node_capacity.accounting.cpu_cores - local.resources.munge.cpu - local.resources.mariadb.cpu
            memory            = var.node_capacity.accounting.memory_gibibytes - local.resources.munge.memory - local.resources.mariadb.memory
            ephemeral_storage = var.node_capacity.accounting.ephemeral_storage_gibibytes - local.resources.munge.ephemeral_storage - local.resources.mariadb.ephemeral_storage
          } : null
        }

        controller = {
          size = var.node_count.controller
          resources = {
            cpu = floor(
              var.node_capacity.controller.cpu_cores
              -local.resources.munge.cpu
              -(var.sssd_enabled ? local.resources.sssd.cpu : 0)
              -local.resources.kruise_daemon.cpu
            )
            memory = floor(
              var.node_capacity.controller.memory_gibibytes
              -local.resources.munge.memory
              -(var.sssd_enabled ? local.resources.sssd.memory : 0)
              -local.resources.kruise_daemon.memory
            )
            ephemeral_storage = floor(
              var.node_capacity.controller.ephemeral_storage_gibibytes
              -local.resources.munge.ephemeral_storage
              -(var.sssd_enabled ? local.resources.sssd.ephemeral_storage : 0)
            )
          }
        }

        worker = {
          size = 0
          resources = {
            cpu = floor(
              var.node_capacity.worker[0].cpu_cores
              -local.resources.munge.cpu
              -(var.sssd_enabled ? local.resources.sssd.cpu : 0)
            ) - local.resources.kruise_daemon.cpu
            memory = floor(
              var.node_capacity.worker[0].memory_gibibytes
              -local.resources.munge.memory
              -(var.sssd_enabled ? local.resources.sssd.memory : 0)
            ) - local.resources.kruise_daemon.memory
            ephemeral_storage = floor(
              var.node_capacity.worker[0].ephemeral_storage_gibibytes
              -local.resources.munge.ephemeral_storage
              -(var.sssd_enabled ? local.resources.sssd.ephemeral_storage : 0)
            )
            gpus = var.node_capacity.worker[0].gpus
          }
          shared_memory            = var.shared_memory_size_gibibytes
          slurm_node_extra         = local.slurm_node_extra
          sshd_config_map_ref_name = var.worker_sshd_config_map_ref_name
        }

        login = {
          size                     = var.node_count.login
          autoscaling              = var.login_autoscaling
          k8s_node_filter_name     = var.login_on_worker_nodes ? local.node_filters.worker.name : local.node_filters.login.name
          allocation_id            = var.login_allocation_id
          sshd_config_map_ref_name = var.login_sshd_config_map_ref_name
          root_public_keys         = var.login_ssh_root_public_keys
          public_ip                = var.login_public_ip
          resources = {
            cpu = floor(
              var.node_capacity.login.cpu_cores
              -local.resources.munge.cpu
              -(var.sssd_enabled ? local.resources.sssd.cpu : 0)
              -local.resources.kruise_daemon.cpu
            )
            memory = floor(
              var.node_capacity.login.memory_gibibytes
              -local.resources.munge.memory
              -(var.sssd_enabled ? local.resources.sssd.memory : 0)
              -local.resources.kruise_daemon.memory
            )
            ephemeral_storage = floor(
              var.node_capacity.login.ephemeral_storage_gibibytes
              -local.resources.munge.ephemeral_storage
              -(var.sssd_enabled ? local.resources.sssd.ephemeral_storage : 0)
            )
          }
        }

        exporter = {
          enabled                = var.exporter_enabled
          resources              = local.resources.exporter
          max_collector_inflight = var.exporter_max_collector_inflight
        }

        munge = {
          resources = local.resources.munge
        }

        sssd = {
          enabled                     = var.sssd_enabled
          conf_secret_ref_name        = var.sssd_conf_secret_ref_name
          ldap_ca_config_map_ref_name = var.sssd_ldap_ca_config_map_ref_name
          resources                   = local.resources.sssd
        }

        rest = {
          enabled   = var.rest_enabled
          resources = local.resources.rest
        }
      }

      sconfigcontroller = {
        node = {
          k8s_node_filter_name = var.sconfigcontroller.node.k8s_node_filter_name
          size                 = var.sconfigcontroller.node.size
        }
        container = {
          image_pull_policy = var.sconfigcontroller.container.image_pull_policy
          resources = {
            cpu               = var.sconfigcontroller.container.resources.cpu
            memory            = var.sconfigcontroller.container.resources.memory
            ephemeral_storage = var.sconfigcontroller.container.resources.ephemeral_storage
          }
        }
      }

    }

    resources = {
      vm_single                   = local.selected_preset.vm_single
      vm_agent                    = local.selected_preset.vm_agent
      vm_logs                     = local.selected_preset.vm_logs
      logs_collector              = local.selected_preset.logs_collector
      jail_logs_collector         = local.selected_preset.jail_logs_collector
      events_collector            = local.selected_preset.events_collector
      nccl_profiles_collector     = local.selected_preset.nccl_profiles_collector
      node_configurator           = local.resources.node_configurator
      soperator_main_controller   = local.resources.soperator_main_controller
      soperator_checks_controller = local.resources.soperator_checks_controller
      dcgm_exporter               = local.resources.dcgm_exporter
      nfs_server                  = local.resources.nfs_server
      spo                         = local.resources.spo
      kruise_manager              = local.selected_preset.kruise_manager
      kube_state_metrics          = local.selected_preset.kube_state_metrics
    }

    vm_agent_queue_count = local.vm_agent_queue_count

    kube_state_metrics_max_scrape_size = local.kube_state_metrics_max_scrape_size

    slurm_nodesets_partitions = var.slurm_nodesets_partitions
    nodesets                  = var.worker_nodesets

    releases = [
      local_file.flux_release_rendered_nodesets.content,
    ]
  })]
}

resource "helm_release" "soperator_fluxcd_bootstrap" {
  depends_on = [
    helm_release.soperator_fluxcd_cm,
  ]

  name       = "soperator-fluxcd-bootstrap"
  repository = var.operator_stable ? "oci://cr.eu-north1.nebius.cloud/soperator" : "oci://cr.eu-north1.nebius.cloud/soperator-unstable"
  chart      = "helm-soperator-fluxcd-bootstrap"
  version    = var.operator_version
  namespace  = var.flux_namespace

  set {
    name  = "helmRepository.url"
    value = var.operator_stable ? "oci://cr.eu-north1.nebius.cloud/soperator" : "oci://cr.eu-north1.nebius.cloud/soperator-unstable"
  }
}

resource "helm_release" "soperator_fluxcd_ad_hoc_cm" {
  name       = "soperator-fluxcd-values"
  repository = local.helm.repository.raw
  chart      = local.helm.chart.raw
  version    = local.helm.version.raw
  namespace  = var.flux_namespace

  values = [templatefile("${path.module}/templates/helm_values/soperator_fluxcd.yaml.tftpl", {})]

  lifecycle {
    ignore_changes = all
  }
}
