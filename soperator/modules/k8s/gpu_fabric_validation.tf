locals {
  worker_gpu_fabric_checks = {
    for idx, worker in var.node_group_workers_v2 :
    idx => {
      name            = worker.name
      preset          = worker.resource.preset
      platform        = worker.resource.platform
      is_gpu          = module.resources.by_platform[worker.resource.platform][worker.resource.preset].gpus > 0
      has_gpu_cluster = worker.gpu_cluster != null
      cluster_id      = worker.gpu_cluster != null ? try(trimspace(worker.gpu_cluster.id), "") : ""
      fabric          = worker.gpu_cluster != null ? try(trimspace(worker.gpu_cluster.infiniband_fabric), "") : ""
    }
  }
}

resource "terraform_data" "check_worker_gpu_fabric" {
  for_each = local.worker_gpu_fabric_checks

  lifecycle {
    precondition {
      condition = (
        each.value.is_gpu
        ? (each.value.has_gpu_cluster && (length(each.value.cluster_id) > 0 || length(each.value.fabric) > 0))
        : !each.value.has_gpu_cluster
      )
      error_message = (
        each.value.is_gpu
        ? "Worker '${each.value.name}' uses GPU preset '${each.value.preset}' and requires either gpu_cluster.id for an existing GPU cluster or gpu_cluster.infiniband_fabric to create one."
        : "Worker '${each.value.name}' uses CPU preset '${each.value.preset}', so gpu_cluster must be unset."
      )
    }
  }
}
