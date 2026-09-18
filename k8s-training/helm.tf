module "network-operator" {
  depends_on = [
    nebius_mk8s_v1_node_group.cpu-only,
    nebius_mk8s_v1_node_group.gpu,
    nebius_mk8s_v1_node_group.gb300,
  ]
  source     = "../modules/network-operator"
  parent_id  = var.parent_id
  cluster_id = nebius_mk8s_v1_cluster.k8s-cluster.id
}

module "gpu-operator" {
  count = (!local.use_driverfull_gpu && !var.custom_driver) ? 1 : 0

  depends_on = [
    module.network-operator
  ]
  source       = "../modules/gpu-operator"
  parent_id    = var.parent_id
  cluster_id   = nebius_mk8s_v1_cluster.k8s-cluster.id
  mig_strategy = var.mig_strategy
  cdi_enabled  = local.gpu_operator_cdi_enabled
}

module "gpu-operator-custom" {
  count = var.custom_driver ? 1 : 0
  depends_on = [
    module.network-operator
  ]
  source       = "../modules/gpu-operator-custom"
  mig_strategy = var.mig_strategy != null ? var.mig_strategy : "none"
  cdi_enabled  = local.gpu_operator_cdi_enabled
}


module "device-plugin" {
  count = local.use_driverfull_gpu ? 1 : 0

  source     = "../modules/device-plugin"
  parent_id  = var.parent_id
  cluster_id = nebius_mk8s_v1_cluster.k8s-cluster.id
}

module "o11y" {
  source                    = "../modules/o11y"
  parent_id                 = var.parent_id
  tenant_id                 = var.tenant_id
  cluster_id                = nebius_mk8s_v1_cluster.k8s-cluster.id
  k8s_node_group_sa_id      = var.enable_k8s_node_group_sa ? nebius_iam_v1_service_account.k8s_node_group_sa[0].id : null
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
      enabled            = var.loki.enabled
      replication_factor = var.loki.replication_factor
      region             = var.region
    }
    prometheus = {
      enabled = var.enable_prometheus
      pv_size = "25Gi"
    }
  }
  test_mode = var.test_mode
}

module "nccl-test" {
  count = var.test_mode && !local.gb300_enabled ? 1 : 0
  depends_on = [
    module.gpu-operator,
  ]
  source          = "../modules/nccl-test"
  number_of_hosts = nebius_mk8s_v1_node_group.gpu[0].fixed_node_count
}
