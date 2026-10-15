resource "helm_release" "this" {
  name             = "kueue"
  repository       = "oci://registry.k8s.io/kueue/charts"
  chart            = "kueue"
  version          = var.chart_version
  namespace        = var.namespace
  create_namespace = true
  atomic           = true
  wait             = true
  timeout          = var.timeout_seconds

  values = concat([
    yamlencode({
      controllerManager = {
        featureGates = [{
          name    = "TopologyAwareScheduling"
          enabled = var.topology_aware_scheduling
        }]
        nodeSelector = var.controller_node_selector
      }
    })
  ], var.helm_values)
}

resource "kubectl_manifest" "topology" {
  count = var.topology_aware_scheduling ? 1 : 0

  yaml_body = yamlencode({
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "Topology"
    metadata = {
      name = var.topology_name
    }
    spec = {
      levels = [
        for node_label in var.topology_levels : {
          nodeLabel = node_label
        }
      ]
    }
  })

  depends_on = [helm_release.this]
}

resource "kubectl_manifest" "resource_flavor" {
  for_each = var.topology_aware_scheduling ? var.resource_flavors : {}

  yaml_body = yamlencode({
    apiVersion = "kueue.x-k8s.io/v1beta2"
    kind       = "ResourceFlavor"
    metadata = {
      name = each.key
    }
    spec = merge(
      {
        nodeLabels   = each.value.node_labels
        topologyName = var.topology_name
      },
      length(each.value.tolerations) > 0 ? {
        tolerations = each.value.tolerations
      } : {},
    )
  })

  depends_on = [kubectl_manifest.topology]
}
