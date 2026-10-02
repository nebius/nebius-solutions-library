resource "helm_release" "gpu_operator" {
  count = local.deploy_gpu_infrastructure_effective ? 1 : 0

  name            = "gpu-operator"
  namespace       = var.gpu_operator_namespace
  repository      = "https://helm.ngc.nvidia.com/nvidia"
  chart           = "gpu-operator"
  version         = var.gpu_operator_chart_version
  values          = [file("${path.module}/../config/helm/gpu-operator.yaml")]
  atomic          = true
  cleanup_on_fail = true
  timeout         = 1200
  set = concat(
    [
      {
        name  = "dcgmExporter.serviceMonitor.enabled"
        value = var.deploy_observability ? "true" : "false"
      }
    ],
    !local.infra_gpu_nodes_driverfull_image && var.gpu_driver_version != null ? [
      {
        name  = "driver.version"
        value = var.gpu_driver_version
      }
    ] : [],
    local.infra_gpu_nodes_driverfull_image ? [
      {
        name  = "driver.enabled"
        value = "false"
      }
    ] : []
  )

  depends_on = [
    terraform_data.validate,
    kubernetes_namespace_v1.gpu_operator,
    helm_release.prometheus,
  ]
}

resource "helm_release" "network_operator" {
  count = local.deploy_gpu_infrastructure_effective && var.deploy_network_operator ? 1 : 0

  name            = "network-operator"
  namespace       = var.network_operator_namespace
  repository      = "https://helm.ngc.nvidia.com/nvidia"
  chart           = "network-operator"
  version         = var.network_operator_chart_version
  values          = [file("${path.module}/../config/helm/network-operator.yaml")]
  atomic          = true
  cleanup_on_fail = true
  timeout         = 1200

  depends_on = [
    terraform_data.validate,
    kubernetes_namespace_v1.network_operator,
  ]
}

resource "helm_release" "kai_scheduler" {
  count = local.deploy_gpu_infrastructure_effective ? 1 : 0

  name            = "kai-scheduler"
  namespace       = var.kai_scheduler_namespace
  repository      = "oci://ghcr.io/nvidia/kai-scheduler"
  chart           = "kai-scheduler"
  version         = var.kai_scheduler_chart_version
  values          = [file("${path.module}/../config/helm/kai-scheduler.yaml")]
  atomic          = true
  cleanup_on_fail = true
  timeout         = 900

  depends_on = [
    terraform_data.validate,
    kubernetes_namespace_v1.kai_scheduler,
  ]
}
