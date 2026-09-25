resource "random_password" "grafana_admin_password" {
  count = var.deploy_observability ? 1 : 0

  length  = 24
  special = false
}

locals {
  grafana_admin_password_value = (
    var.grafana_admin_password != null && var.grafana_admin_password != ""
    ? var.grafana_admin_password
    : try(random_password.grafana_admin_password[0].result, "")
  )
}

resource "terraform_data" "cleanup_stale_prometheus_release" {
  count = var.deploy_observability ? 1 : 0

  input = {
    namespace    = var.monitoring_namespace
    release_name = "prometheus"
  }

  triggers_replace = {
    namespace    = var.monitoring_namespace
    release_name = "prometheus"
  }

  depends_on = [
    kubernetes_namespace_v1.monitoring,
  ]

  provisioner "local-exec" {
    command = "/bin/bash ${path.module}/../scripts/cleanup-stale-helm-release.sh"

    environment = {
      KUBECONFIG      = pathexpand(var.kubeconfig_path)
      KUBECTL_CONTEXT = var.kubeconfig_context != null ? var.kubeconfig_context : ""
      NAMESPACE       = var.monitoring_namespace
      RELEASE_NAME    = "prometheus"
    }
  }
}

resource "helm_release" "prometheus" {
  count = var.deploy_observability ? 1 : 0

  name            = "prometheus"
  namespace       = var.monitoring_namespace
  repository      = "https://prometheus-community.github.io/helm-charts"
  chart           = "kube-prometheus-stack"
  version         = var.kube_prometheus_stack_chart_version
  values          = [file("${path.module}/../config/helm/prometheus.yaml")]
  atomic          = true
  cleanup_on_fail = true
  timeout         = 2400
  set_sensitive = [
    {
      name  = "grafana.adminPassword"
      value = local.grafana_admin_password_value
    }
  ]

  depends_on = [
    terraform_data.validate,
    terraform_data.cleanup_stale_prometheus_release,
    kubernetes_namespace_v1.monitoring,
  ]
}

resource "helm_release" "loki" {
  count = var.deploy_observability ? 1 : 0

  name            = "loki"
  namespace       = var.monitoring_namespace
  repository      = "https://grafana.github.io/helm-charts"
  chart           = "loki-stack"
  version         = var.loki_stack_chart_version
  values          = [file("${path.module}/../config/helm/loki.yaml")]
  atomic          = true
  cleanup_on_fail = true
  timeout         = 1800

  depends_on = [
    terraform_data.validate,
    kubernetes_namespace_v1.monitoring,
  ]
}

resource "helm_release" "promtail" {
  count = var.deploy_observability ? 1 : 0

  name            = "promtail"
  namespace       = var.monitoring_namespace
  repository      = "https://grafana.github.io/helm-charts"
  chart           = "promtail"
  version         = var.promtail_chart_version
  values          = [file("${path.module}/../config/helm/promtail.yaml")]
  atomic          = true
  cleanup_on_fail = true
  timeout         = 900

  depends_on = [
    helm_release.prometheus,
    helm_release.loki,
  ]
}
