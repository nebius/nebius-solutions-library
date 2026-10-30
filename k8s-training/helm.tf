module "network-operator" {
  depends_on = [
    nebius_mk8s_v1_node_group.cpu-only,
    nebius_mk8s_v1_node_group.gpu,
    nebius_mk8s_v1_node_group.gb300,
  ]
  source     = "../modules/network-operator"
  parent_id  = var.parent_id
  cluster_id = nebius_mk8s_v1_cluster.k8s-cluster.id
  values     = var.network_operator_values
}

module "gpu-operator" {
  count = (!local.use_driverfull_gpu || local.runai_worker) && !var.custom_driver ? 1 : 0

  depends_on = [
    module.network-operator,
  ]
  source               = "../modules/gpu-operator"
  parent_id            = var.parent_id
  cluster_id           = nebius_mk8s_v1_cluster.k8s-cluster.id
  managed_image        = local.use_driverfull_gpu
  values               = var.gpu_operator_values
  mig_strategy         = var.mig_strategy
  cdi_enabled          = local.use_driverfull_gpu ? false : local.gpu_operator_cdi_enabled
  enable_dcgm_exporter = local.runai_worker && !local.use_driverfull_gpu
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
  # MK8s disables its built-in plugin for DRA. Run:ai uses the managed image plugin.
  count = local.use_driverfull_gpu && !local.gb300_dra_enabled && !var.enable_runai ? 1 : 0

  source     = "../modules/device-plugin"
  parent_id  = var.parent_id
  cluster_id = nebius_mk8s_v1_cluster.k8s-cluster.id
}

module "o11y" {
  source                    = "../modules/o11y"
  parent_id                 = var.parent_id
  tenant_id                 = var.tenant_id
  cluster_name              = var.cluster_name
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
  image           = var.nccl_test_image
}

# Optional upstream controllers. Application workloads and APIs are separate.
locals {
  runai_worker = var.enable_runai && try(var.runai.role == "worker", false)
}

module "runai" {
  count           = var.enable_runai ? 1 : 0
  source          = "../modules/runai"
  config          = var.runai
  bootstrap_token = local.runai_worker ? sensitive(file(var.runai.bootstrap_token_file)) : null
  depends_on      = [module.network-operator, module.gpu-operator, module.gpu-operator-custom, kubernetes_secret_v1.platform]
}

module "dynamo" {
  count  = var.enable_dynamo ? 1 : 0
  source = "../modules/dynamo"
  config = merge(var.dynamo, {
    scheduler_mode = local.runai_worker ? "runai" : try(var.dynamo.scheduler_mode, "standalone")
  })
  depends_on = [module.runai, module.gpu-operator, module.gpu-operator-custom]
}

module "mpi_operator" {
  count  = var.enable_mpi_operator ? 1 : 0
  source = "../modules/mpi-operator"
  config = var.mpi_operator
}

module "opencost" {
  count          = var.enable_opencost ? 1 : 0
  source         = "../modules/opencost"
  prometheus_url = var.opencost.prometheus_url
  namespace      = var.opencost.namespace
  chart_version  = var.opencost.chart_version
  chart_path     = var.opencost.chart_path
  values         = [for filename in var.opencost.values_files : file(filename)]
  depends_on     = [module.o11y, module.runai]
}

module "nim_operator" {
  count      = var.enable_nim_operator ? 1 : 0
  source     = "../modules/nim-operator"
  config     = var.nim_operator
  depends_on = [module.runai, module.gpu-operator, module.gpu-operator-custom]
}

# Run:ai owns its project namespaces. Declare only other prerequisite namespaces.
resource "kubernetes_namespace_v1" "platform" {
  for_each = var.platform_namespaces
  metadata { name = each.value }
  lifecycle {
    # Native operators own namespace metadata; this resource owns its existence.
    ignore_changes = [metadata[0].labels, metadata[0].annotations]
  }
  depends_on = [nebius_mk8s_v1_node_group.cpu-only]
}

resource "kubernetes_secret_v1" "platform" {
  for_each = var.platform_secrets
  metadata {
    name      = each.value.name
    namespace = each.value.namespace
    labels    = each.value.labels
  }
  type             = each.value.type
  data_wo          = { for key, filename in each.value.data_files : key => file(filename) }
  data_wo_revision = each.value.revision
  depends_on       = [kubernetes_namespace_v1.platform]
}

# Optional shared infrastructure charts; model releases belong to the external application.
resource "helm_release" "platform" {
  for_each         = var.platform_releases
  name             = each.key
  namespace        = each.value.namespace
  repository       = each.value.repository
  chart            = each.value.chart
  version          = each.value.version
  create_namespace = false
  values           = [for filename in each.value.values_files : file(filename)]
  atomic           = true
  wait_for_jobs    = true
  timeout          = each.value.timeout
  depends_on       = [module.runai, module.dynamo, module.nim_operator, kubernetes_secret_v1.platform]
}
