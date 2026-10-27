moved {
  from = kubernetes_config_map_v1.worker_nccl_network_profile
  to   = kubernetes_config_map_v1.worker_nccl_network_vars
}

resource "kubernetes_config_map_v1" "worker_nccl_network_vars" {
  for_each = local.worker_nccl_network_vars

  metadata {
    name      = each.value.config_map_name
    namespace = "soperator"
  }

  data = {
    (each.value.file_name) = each.value.content
  }

  depends_on = [
    terraform_data.wait_for_slurm_cluster_hr,
  ]
}
