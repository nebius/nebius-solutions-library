locals {
  nim_hpa_models = {
    for name, model in local.nim_models : name => model
    if model.enabled && model.scaling.enabled
  }

  nim_service_monitor_models = {
    for name, model in local.nim_models : name => model
    if model.service_monitor.enabled
  }
}

resource "kubernetes_horizontal_pod_autoscaler_v2" "nims" {
  for_each = local.nim_hpa_models

  depends_on = [kubernetes_deployment_v1.nims]

  metadata {
    name      = each.value.deployment_name
    namespace = var.namespace

    labels = {
      app                          = each.value.app
      "app.kubernetes.io/name"     = each.value.deployment_name
      "app.kubernetes.io/part-of"  = "nims"
      "app.kubernetes.io/instance" = each.key
    }
  }

  spec {
    min_replicas = each.value.scaling.min_replicas
    max_replicas = each.value.scaling.max_replicas

    scale_target_ref {
      api_version = "apps/v1"
      kind        = "Deployment"
      name        = kubernetes_deployment_v1.nims[each.key].metadata[0].name
    }

    metric {
      type = each.value.scaling.metric_type

      dynamic "pods" {
        for_each = each.value.scaling.metric_type == "Pods" ? [each.value.scaling] : []
        content {
          metric {
            name = pods.value.metric_name
          }
          target {
            type          = pods.value.target_type
            average_value = pods.value.threshold
          }
        }
      }

      dynamic "external" {
        for_each = each.value.scaling.metric_type == "External" ? [each.value.scaling] : []
        content {
          metric {
            name = external.value.metric_name
          }
          target {
            type          = external.value.target_type
            value         = external.value.target_type == "Value" ? external.value.threshold : null
            average_value = external.value.target_type == "AverageValue" ? external.value.threshold : null
          }
        }
      }
    }
  }
}

resource "kubernetes_manifest" "nim_service_monitor" {
  for_each = local.nim_service_monitor_models

  depends_on = [kubernetes_service_v1.nims]

  manifest = {
    apiVersion = "monitoring.coreos.com/v1"
    kind       = "ServiceMonitor"
    metadata = {
      name      = "${each.value.deployment_name}-metrics"
      namespace = var.namespace
      labels = merge(
        {
          app                          = each.value.app
          "app.kubernetes.io/name"     = each.value.deployment_name
          "app.kubernetes.io/part-of"  = "nims"
          "app.kubernetes.io/instance" = each.key
        },
        var.service_monitor_labels
      )
    }
    spec = {
      selector = {
        matchLabels = {
          app                          = each.value.app
          "app.kubernetes.io/part-of"  = "nims"
          "app.kubernetes.io/instance" = each.key
        }
      }
      namespaceSelector = {
        matchNames = [var.namespace]
      }
      endpoints = [
        {
          port          = each.value.service_monitor.port
          path          = each.value.service_monitor.path
          interval      = var.service_monitor_interval
          scrapeTimeout = var.service_monitor_scrape_timeout
        }
      ]
    }
  }
}
