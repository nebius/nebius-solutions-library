data "kubernetes_service_v1" "ingress_controller" {
  count = var.enable_auth ? 1 : 0

  metadata {
    name      = coalesce(var.ingress_controller_service_name, "${var.ingress_release_name}-controller")
    namespace = var.ingress_namespace
  }

  depends_on = [
    terraform_data.ingress_ready,
  ]
}
