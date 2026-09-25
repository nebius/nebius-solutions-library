resource "terraform_data" "ingress_ready" {
  input = {
    release_status = try(helm_release.ingress_nginx[0].status, "external")
  }
}

resource "helm_release" "ingress_nginx" {
  count      = var.deploy_ingress_nginx ? 1 : 0
  name       = var.ingress_release_name
  namespace  = var.ingress_namespace
  repository = "https://kubernetes.github.io/ingress-nginx"
  chart      = "ingress-nginx"
  version    = var.ingress_nginx_chart_version
  values     = [yamlencode(local.ingress_nginx_values)]
  timeout    = 300

  depends_on = [
    kubernetes_namespace_v1.ingress,
  ]
}
