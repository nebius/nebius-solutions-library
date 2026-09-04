module "kuberay" {
  source = "../modules/kuberay"
  count  = var.enable_kuberay_cluster ? 1 : 0

  depends_on = [
    nebius_mk8s_v1_node_group.cpu-only,
    nebius_mk8s_v1_node_group.gpu,
    nebius_mk8s_v1_node_group.gb300,
    module.network-operator,
    module.gpu-operator,
  ]

  parent_id  = var.parent_id
  cluster_id = nebius_mk8s_v1_cluster.k8s-cluster.id
  #cpu worker setup
  cpu_platform     = local.cpu_nodes_platform
  cpu_worker_image = var.kuberay_cpu_worker_image
  min_cpu_replicas = var.kuberay_min_cpu_replicas
  max_cpu_replicas = var.kuberay_max_cpu_replicas
  cpu_resources    = var.kuberay_cpu_resources
  #gpu worker setup
  gpu_platform     = local.gpu_nodes_platform
  gpu_worker_image = var.kuberay_gpu_worker_image
  min_gpu_replicas = var.kuberay_min_gpu_replicas
  max_gpu_replicas = var.kuberay_max_gpu_replicas
  gpu_resources    = var.kuberay_gpu_resources

}

module "kuberay-service" {
  source = "../modules/kuberay-service"
  count  = var.enable_kuberay_service ? 1 : 0

  depends_on = [
    nebius_mk8s_v1_node_group.cpu-only,
    nebius_mk8s_v1_node_group.gpu,
    nebius_mk8s_v1_node_group.gb300,
    module.network-operator,
    module.gpu-operator,
  ]

  parent_id        = var.parent_id
  cluster_id       = nebius_mk8s_v1_cluster.k8s-cluster.id
  cpu_platform     = local.cpu_nodes_platform
  gpu_platform     = local.gpu_nodes_platform
  min_gpu_replicas = var.kuberay_min_gpu_replicas
  max_gpu_replicas = var.kuberay_max_gpu_replicas
  serve_config_v2  = var.kuberay_serve_config_v2
}

module "kueue" {
  count  = var.kueue.enabled ? 1 : 0
  source = "../modules/kueue"

  chart_version             = var.kueue.chart_version
  namespace                 = var.kueue.namespace
  timeout_seconds           = var.kueue.timeout_seconds
  helm_values               = var.kueue.helm_values
  topology_aware_scheduling = var.kueue.topology_aware_scheduling
  controller_node_selector = {
    "nebius.com/node-group-id" = nebius_mk8s_v1_node_group.cpu-only.id
  }
  resource_flavors = merge(
    {
      for index, node_group in nebius_mk8s_v1_node_group.gpu :
      "nebius-gpu-${index}" => {
        node_labels = {
          "nebius.com/node-group-id" = node_group.id
        }
        tolerations = [{
          key      = "nvidia.com/gpu"
          operator = "Exists"
          effect   = "NoSchedule"
        }]
      }
    },
    {
      for index, rack in sort(keys(nebius_mk8s_v1_node_group.gb300)) :
      "nebius-gpu-${index}" => {
        node_labels = {
          "nebius.com/node-group-id" = nebius_mk8s_v1_node_group.gb300[rack].id
        }
        tolerations = [{
          key      = "nvidia.com/gpu"
          operator = "Exists"
          effect   = "NoSchedule"
        }]
      }
    },
  )

  depends_on = [
    nebius_mk8s_v1_node_group.cpu-only,
  ]
}

module "opa_gatekeeper" {
  source = "../modules/opa_gatekeeper"
  count  = var.opa_gatekeeper_enable ? 1 : 0
}

resource "terraform_data" "binpacking_scheduler_version" {
  count = var.binpacking_enable ? 1 : 0

  lifecycle {
    precondition {
      condition     = local.binpacking_kube_sched_ver != null
      error_message = "When binpacking_enable is true, set k8s_version to a supported minor version or set binpacking_kube_sched_ver to a full kube-scheduler patch version."
    }

    precondition {
      condition     = !local.binpacking_enable_mutator || var.opa_gatekeeper_enable
      error_message = "binpacking_forced_namespaces requires opa_gatekeeper_enable = true. Set opa_gatekeeper_enable = true, or set binpacking_forced_namespaces = [] and opt pods in with spec.schedulerName."
    }
  }
}

module "binpacking_scheduler" {
  source         = "../modules/binpacking"
  count          = var.binpacking_enable && local.binpacking_kube_sched_ver != null ? 1 : 0
  kube_sched_ver = local.binpacking_kube_sched_ver
}

resource "kubectl_manifest" "binpacking_mutator" {
  count = (
    var.binpacking_enable &&
    local.binpacking_enable_mutator &&
    var.opa_gatekeeper_enable
  ) ? 1 : 0

  yaml_body = templatefile("../modules/binpacking/files/opa_gatekeeper_mutator.yaml.tftpl", {
    namespaces = var.binpacking_forced_namespaces
  })

  depends_on = [
    module.opa_gatekeeper,
    module.binpacking_scheduler,
  ]
}
