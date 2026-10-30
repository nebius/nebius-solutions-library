locals {
  worker    = var.config.role == "worker"
  namespace = local.worker ? "runai" : "runai-backend"
  serving   = local.worker && var.config.serving != null
}

resource "helm_release" "ingress" {
  count            = var.config.install_ingress ? 1 : 0
  name             = "haproxy"
  namespace        = "haproxy-controller"
  repository       = var.config.ingress_chart_path == null ? "https://haproxytech.github.io/helm-charts" : null
  chart            = coalesce(var.config.ingress_chart_path, "kubernetes-ingress")
  version          = "1.54.3"
  create_namespace = true
  atomic           = true
  wait_for_jobs    = true
  timeout          = 1200
  values = concat([yamlencode({ controller = {
    ingressClass         = var.config.ingress_class
    ingressClassResource = { enabled = true }
    service              = { type = "LoadBalancer" }
  } })], [for filename in var.config.ingress_values_files : file(filename)])
}

resource "helm_release" "prometheus_operator" {
  count            = local.worker && var.config.install_prometheus_operator ? 1 : 0
  name             = "kube-prometheus-stack"
  namespace        = "monitoring"
  repository       = var.config.prometheus_chart_path == null ? "https://prometheus-community.github.io/helm-charts" : null
  chart            = coalesce(var.config.prometheus_chart_path, "kube-prometheus-stack")
  version          = "88.6.5"
  create_namespace = true
  atomic           = true
  wait_for_jobs    = true
  timeout          = 1200
  values = concat([yamlencode({
    prometheus       = { enabled = false }
    grafana          = { enabled = false }
    alertmanager     = { enabled = false }
    nodeExporter     = { enabled = false }
    kubeStateMetrics = { enabled = false }
    defaultRules     = { create = false }
  })], [for filename in var.config.prometheus_values_files : file(filename)])
}

resource "helm_release" "this" {
  name             = local.worker ? "runai-cluster" : "runai-backend"
  namespace        = local.namespace
  repository       = var.config.chart_path == null ? "https://helm.ngc.nvidia.com/nvidia/runai-oss" : null
  chart            = coalesce(var.config.chart_path, local.worker ? "runai-cluster" : "control-plane")
  version          = var.config.version
  create_namespace = true
  atomic           = true
  wait_for_jobs    = true
  timeout          = 2400

  values = concat(
    # Fractions need independently qualified drivers; ordinary whole-GPU work is the baseline.
    local.worker ? [yamlencode({ kai-resource-management = { kai-scheduler = { global = { nvFractions = { set = false } } } } })] : [],
    local.serving ? [yamlencode({ clusterConfig = { pod-grouper = { args = { gangScheduleKnative = false } } } })] : [],
    [for filename in var.config.values_files : file(filename)],
    [local.worker ? yamlencode({
      controlPlane  = { url = var.config.control_plane_url }
      cluster       = { uid = var.config.cluster_uid, url = "https://${var.config.domain}" }
      clusterConfig = { global = { ingress = { ingressClass = var.config.ingress_class } } }
    }) : yamlencode({ global = { domain = var.config.domain, ingress = { ingressClass = var.config.ingress_class } } })]
  )

  set_wo = local.worker ? [{
    name  = "controlPlane.bootstrapToken"
    value = var.bootstrap_token
  }] : []
  set_wo_revision = local.worker ? var.config.bootstrap_token_revision : null
  depends_on      = [helm_release.ingress, helm_release.prometheus_operator, helm_release.serving]
}
