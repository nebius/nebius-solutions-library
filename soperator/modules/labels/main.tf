locals {
  const = {
    domain = {
      slurm  = "slurm.nebius.ai"
      nebius = "nebius.com"
      nvidia = "nvidia.com"
    }

    name = {
      jail    = "jail"
      nodeset = "nodeset"
      nodesets = {
        system     = "system"
        controller = "controller"
        worker     = "worker"
        login      = "login"
        accounting = "accounting"
        nfs        = "nfs"
      }

      workload = "workload"
      workloads = {
        cpu = "cpu"
        gpu = "gpu"
      }
    }
  }

  label_key = {
    nebius_gpu = "${local.const.domain.nebius}/${local.const.name.workloads.gpu}"
    nvidia_gpu = "${local.const.domain.nvidia}/${local.const.name.workloads.gpu}"

    nebius_nvlink_instance_group = "${local.const.domain.nebius}/nvlink-instance-group"

    slurm_nodeset      = "${local.const.domain.slurm}/${local.const.name.nodeset}"
    slurm_nodeset_name = "${local.const.domain.slurm}/${local.const.name.nodeset}-name"
    slurm_workload     = "${local.const.domain.slurm}/${local.const.name.workload}"
    jail               = "${local.const.domain.slurm}/${local.const.name.jail}"
  }

  label = {
    nebius_gpu = tomap({ (local.label_key.nebius_gpu) = ("true") })

    jail = tomap({ (local.label_key.jail) = ("true") })

    exclude_from_external_lb = tomap({ ("node.kubernetes.io/exclude-from-external-load-balancers") = ("true") })

    nodeset = {
      system     = tomap({ (local.label_key.slurm_nodeset) = (local.const.name.nodesets.system) })
      controller = tomap({ (local.label_key.slurm_nodeset) = (local.const.name.nodesets.controller) })
      worker     = tomap({ (local.label_key.slurm_nodeset) = (local.const.name.nodesets.worker) })
      login      = tomap({ (local.label_key.slurm_nodeset) = (local.const.name.nodesets.login) })
      accounting = tomap({ (local.label_key.slurm_nodeset) = (local.const.name.nodesets.accounting) })
      nfs        = tomap({ (local.label_key.slurm_nodeset) = (local.const.name.nodesets.nfs) })
    }

    workload = {
      cpu = tomap({ (local.label_key.slurm_workload) = (local.const.name.workloads.cpu) })
      gpu = tomap({ (local.label_key.slurm_workload) = (local.const.name.workloads.gpu) })
    }
  }
}
