locals {
  namespace = {
    monitoring = "monitoring-system"
  }

  repository = {
    raw = {
      repository = "https://bedag.github.io/helm-charts/"
      chart      = "raw"
      version    = "2.0.0"
    }
  }

}
resource "helm_release" "dashboard" {
  for_each = tomap({
    soperator_exporter     = "soperator-exporter"
    kube_state_metrics     = "kube-state-metrics"
    pod_resources          = "pod-resources"
    jobs_overview          = "jobs-overview"
    workers_overview       = "workers-overview"
    workers_detailed_stats = "workers-detailed-stats"
  })

  name       = "${var.slurm_cluster_name}-grafana-dashboard-${each.value}"
  repository = local.repository.raw.repository
  chart      = local.repository.raw.chart
  version    = local.repository.raw.version

  namespace = local.namespace.monitoring

  values = [yamlencode({
    resources = [{
      apiVersion = "v1"
      kind       = "ConfigMap"
      metadata = {
        namespace = local.namespace.monitoring
        name      = "${var.slurm_cluster_name}-${each.value}"
        labels = {
          grafana_dashboard = "1"
        }
      }
      data = {
        "${each.value}.json" = file("${path.module}/templates/dashboards/${each.key}.json")
      }
    }]
  })]

  wait = true
}
