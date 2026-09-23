module "network-operator" {
  depends_on = [
    nebius_mk8s_v1_node_group.cpu-only,
    nebius_mk8s_v1_node_group.gpu,
  ]
  source     = "../../modules/network-operator"
  parent_id  = local.primary_parent_id
  cluster_id = nebius_mk8s_v1_cluster.k8s-cluster[local.primary-cluster-key].id
}

module "gpu-operator" {
  count = !var.gpu_nodes_driverfull_image ? 1 : 0

  depends_on = [
    module.network-operator
  ]
  source       = "../../modules/gpu-operator"
  parent_id    = local.primary_parent_id
  cluster_id   = nebius_mk8s_v1_cluster.k8s-cluster[local.primary-cluster-key].id
  mig_strategy = var.mig_strategy
}


module "device-plugin" {
  count = var.gpu_nodes_driverfull_image ? 1 : 0
  depends_on = [
    nebius_mk8s_v1_node_group.cpu-only,
    nebius_mk8s_v1_node_group.gpu,
  ]

  source     = "../../modules/device-plugin"
  parent_id  = local.primary_parent_id
  cluster_id = nebius_mk8s_v1_cluster.k8s-cluster[local.primary-cluster-key].id
}

module "o11y" {
  depends_on = [
    nebius_mk8s_v1_node_group.cpu-only,
    nebius_mk8s_v1_node_group.gpu,
  ]
  source     = "../../modules/o11y"
  parent_id  = local.primary_parent_id
  tenant_id  = var.tenant_id
  cluster_id = nebius_mk8s_v1_cluster.k8s-cluster[local.primary-cluster-key].id
  #cpu_nodes_count           = var.cpu_nodes_count
  #gpu_nodes_count           = var.gpu_nodes_count_per_group * var.gpu_node_groups
  k8s_node_group_sa_id      = var.enable_k8s_node_group_sa ? nebius_iam_v1_service_account.k8s_node_group_sa[local.primary-cluster-key].id : null
  k8s_node_group_sa_enabled = var.enable_k8s_node_group_sa

  o11y = {
    nebius_o11y_agent = {
      enabled                  = var.enable_nebius_o11y_agent
      collectK8sClusterMetrics = var.collectK8sClusterMetrics
    }
    grafana = {
      enabled = var.enable_grafana
    }
    loki = {
      enabled            = var.enable_loki
      replication_factor = var.loki_custom_replication_factor
      region             = local.primary_region
    }
    prometheus = {
      enabled = var.enable_prometheus
      pv_size = "25Gi"
    }
  }
  test_mode = var.test_mode
}

module "nccl-test" {
  count = var.test_mode ? 1 : 0
  depends_on = [
    module.gpu-operator,
  ]
  source          = "../../modules/nccl-test"
  number_of_hosts = nebius_mk8s_v1_node_group.gpu["${local.primary-cluster-key}-0"].fixed_node_count
}
