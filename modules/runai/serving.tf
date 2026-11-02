resource "helm_release" "serving_operator" {
  count            = local.serving ? (var.config.serving.install_operator ? 1 : 0) : 0
  name             = "knative-operator"
  namespace        = "knative-operator"
  repository       = "https://knative.github.io/operator"
  chart            = "knative-operator"
  version          = var.config.serving.operator_version
  create_namespace = true
  atomic           = true
  wait_for_jobs    = true
  timeout          = 1200
  values           = var.config.serving.operator_values == null ? [] : [var.config.serving.operator_values]
}

resource "kubectl_manifest" "serving" {
  count = local.serving ? 1 : 0
  yaml_body = yamlencode({
    apiVersion = "operator.knative.dev/v1beta1"
    kind       = "KnativeServing"
    metadata   = { name = "knative-serving", namespace = "knative-serving" }
    spec       = yamldecode(var.config.serving.spec)
  })
  depends_on = [helm_release.serving_operator]
}

resource "kubectl_manifest" "serving_ingress" {
  count = local.serving ? 1 : 0
  yaml_body = yamlencode({
    apiVersion = "networking.k8s.io/v1"
    kind       = "Ingress"
    metadata = {
      name        = "knative-serving"
      namespace   = "knative-serving"
      annotations = { "nginx.ingress.kubernetes.io/ssl-redirect" = "true" }
    }
    spec = {
      ingressClassName = var.config.ingress_class
      rules = [{
        host = var.config.serving.inference_domain
        http = { paths = [{
          path     = "/"
          pathType = "Prefix"
          backend  = { service = { name = "kourier", port = { number = 80 } } }
        }] }
      }]
      tls = [{ hosts = [var.config.serving.inference_domain], secretName = var.config.serving.tls_secret_name }]
    }
  })
  depends_on = [kubectl_manifest.serving, nebius_applications_v1alpha1_k8s_release.ingress]
}

data "kubernetes_service_v1" "ingress" {
  count = local.public_endpoint && local.serving && var.config.install_ingress ? 1 : 0
  metadata {
    name      = "runai-ingress-controller"
    namespace = "ingress-nginx"
  }
  depends_on = [nebius_applications_v1alpha1_k8s_release.ingress]
}

resource "kubernetes_service_v1" "inference" {
  count = local.public_endpoint && local.serving && var.config.install_ingress ? 1 : 0
  metadata {
    name      = "runai-inference"
    namespace = data.kubernetes_service_v1.ingress[0].metadata[0].namespace
  }
  spec {
    type     = "LoadBalancer"
    selector = data.kubernetes_service_v1.ingress[0].spec[0].selector
    port {
      name        = "https"
      port        = 443
      target_port = "https"
      protocol    = "TCP"
    }
  }
}
