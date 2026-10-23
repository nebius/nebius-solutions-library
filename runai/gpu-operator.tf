module "gpu-operator" {
  source       = "../modules/gpu-operator"
  parent_id    = var.parent_id
  cluster_id   = nebius_mk8s_v1_cluster.k8s-cluster.id
  mig_strategy = var.mig_strategy

  enable_dcgm_exporter = true

  depends_on = [
    nebius_mk8s_v1_node_group.gpu
  ]
}
