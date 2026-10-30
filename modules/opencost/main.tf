resource "helm_release" "this" {
  name             = "opencost"
  namespace        = var.namespace
  create_namespace = true
  repository       = var.chart_path == null ? "https://opencost.github.io/opencost-helm-chart" : null
  chart            = coalesce(var.chart_path, "opencost")
  version          = var.chart_version
  values = concat([yamlencode({
    opencost = {
      prometheus = {
        internal = { enabled = false }
        external = { enabled = true, url = var.prometheus_url }
      }
    }
  })], var.values)
  wait    = true
  timeout = 600
}
