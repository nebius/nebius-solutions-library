resource "local_file" "flux_release_rendered_nodesets" {
  filename = "${path.root}/assets/render/flux_release_nodesets.yaml"

  content = templatefile("${path.module}/templates/helm_values/flux_release_nodesets.yaml.tftpl", {
    version      = var.operator_version
    namespace    = "soperator"
    release_name = "soperator-nodesets"
    cluster_name = var.name

    jail = {
      on_weka = local.jail_on_weka
    }

    nodesets = [for nodeset in var.worker_nodesets : merge(nodeset, {
      nccl_network_vars = try(local.worker_nccl_network_vars[nodeset.name], null)
      slurm_node_extra  = local.slurm_node_extra_by_nodeset[nodeset.name]
    })]
    resources = [for i, res in var.node_capacity.worker : {
      cpu_cores = floor(
        res.cpu_cores
        -local.resources.munge.cpu
        -(var.sssd_enabled ? local.resources.sssd.cpu : 0)
      ) - local.resources.kruise_daemon.cpu
      memory_gibibytes = floor(
        res.memory_gibibytes
        -local.resources.munge.memory
        -(var.sssd_enabled ? local.resources.sssd.memory : 0)
      ) - local.resources.kruise_daemon.memory
      ephemeral_storage_gibibytes = (
        try(var.worker_nodesets[i].local_nvme.enabled, false) &&
        try(var.worker_nodesets[i].local_nvme.size_limit_gibibytes, null) != null
        ? var.worker_nodesets[i].local_nvme.size_limit_gibibytes
        : floor(
          res.ephemeral_storage_gibibytes
          -local.resources.munge.ephemeral_storage
          -(var.sssd_enabled ? local.resources.sssd.ephemeral_storage : 0)
        )
      )
      gpus          = res.gpus
      shared_memory = var.shared_memory_size_gibibytes
    }]

    jail_submounts = {
      # NFS is used for /home, which is not needed in case of Jail on WEKA
      nfs = {
        vds = (local.jail_on_weka
          ? { enabled = false }
          : var.nfs
        )
        k8s = (local.jail_on_weka
          ? { enabled = false }
          : var.nfs_in_k8s
        )
      }

      shared = [for submount in var.filestores.jail_submounts : {
        name       = submount.name
        mount_path = submount.mount_path
      }]
    }

    gpu = {
      use_preinstalled_drivers = var.use_preinstalled_gpu_drivers
    }

    munge = {
      resources = local.resources.munge
    }

    sshd = {
      config_map_ref = var.worker_sshd_config_map_ref_name
    }

    sssd = {
      enabled                     = var.sssd_enabled
      conf_secret_ref_name        = var.sssd_conf_secret_ref_name
      ldap_ca_config_map_ref_name = var.sssd_ldap_ca_config_map_ref_name
      resources                   = local.resources.sssd
    }
  })
}
