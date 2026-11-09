resource "local_file" "flux_release_rendered_nodesets" {
  filename = "${path.root}/assets/render/flux_release_nodesets.yaml"

  content = templatefile("${path.module}/templates/helm_values/flux_release_nodesets.yaml.tftpl", {
    version          = var.operator_version
    namespace        = "soperator"
    release_name     = "soperator-nodesets"
    cluster_name     = var.name
    apparmor_profile = local.apparmor_profile
    slurm_node_extra = local.slurm_node_extra

    nodesets = [for i, nodeset in var.worker_nodesets : merge(nodeset, {
      name = try(local.worker_nodeset_range_by_first_name[nodeset.name].name, nodeset.name)
      range = try({
        first_ordinal = local.worker_nodeset_range_by_first_name[nodeset.name].first_ordinal
        last_ordinal  = local.worker_nodeset_range_by_first_name[nodeset.name].last_ordinal
        suffix        = local.worker_nodeset_range_by_first_name[nodeset.name].suffix
      }, null)
      nccl_network_vars = try(local.worker_nccl_network_vars[nodeset.platform], null)
      resources = {
        cpu_cores = floor(
          var.node_capacity.worker[i].cpu_cores
          -local.resources.munge.cpu
          -(var.sssd_enabled ? local.resources.sssd.cpu : 0)
        ) - local.resources.kruise_daemon.cpu
        memory_gibibytes = local.worker_memory[i]
        ephemeral_storage_gibibytes = (
          try(nodeset.local_nvme.enabled, false) &&
          try(nodeset.local_nvme.size_limit_gibibytes, null) != null
          ? nodeset.local_nvme.size_limit_gibibytes
          : floor(
            var.node_capacity.worker[i].ephemeral_storage_gibibytes
            -local.resources.munge.ephemeral_storage
            -(var.sssd_enabled ? local.resources.sssd.ephemeral_storage : 0)
          )
        )
        gpus = var.node_capacity.worker[i].gpus
      }
      })
      if !contains(local.worker_nodeset_names_in_ranges, nodeset.name) || contains(keys(local.worker_nodeset_range_by_first_name), nodeset.name)
    ]

    jail_submounts = {
      nfs = {
        vds = var.nfs
        k8s = var.nfs_in_k8s
      }

      shared = [for submount in var.filestores.jail_submounts : {
        name       = submount.name
        mount_path = submount.mount_path
      }]
    }

    gpu = {
      use_preinstalled_drivers = var.use_preinstalled_gpu_drivers
      wait_for_persistenced    = var.wait_for_nvidia_persistenced
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
