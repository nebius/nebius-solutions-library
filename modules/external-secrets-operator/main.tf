resource "kubernetes_namespace_v1" "this" {
  count = var.create_namespace ? 1 : 0

  metadata {
    name = var.namespace
  }
}

resource "helm_release" "this" {
  depends_on = [kubernetes_namespace_v1.this]

  name             = var.release_name
  repository       = var.repository_url
  chart            = var.chart_name
  version          = var.chart_version
  namespace        = var.namespace
  create_namespace = false
  atomic           = var.atomic
  wait             = var.wait
  timeout          = var.timeout_seconds

  values = concat([
    yamlencode({
      installCRDs = var.install_crds
    })
  ], var.values)
}
