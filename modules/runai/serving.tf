resource "helm_release" "serving_operator" {
  count            = local.serving ? (var.config.serving.install_operator ? 1 : 0) : 0
  name             = "knative-operator"
  namespace        = "knative-operator"
  repository       = var.config.serving.operator_chart_path == null ? "https://knative.github.io/operator" : null
  chart            = coalesce(var.config.serving.operator_chart_path, "knative-operator")
  version          = var.config.serving.operator_version
  create_namespace = true
  atomic           = true
  wait_for_jobs    = true
  timeout          = 1200
  values           = [for filename in var.config.serving.operator_values_files : file(filename)]
}

resource "helm_release" "serving" {
  count            = local.serving ? 1 : 0
  name             = "runai-serving"
  namespace        = "knative-serving"
  chart            = "${path.module}/charts/serving"
  create_namespace = true
  atomic           = true
  timeout          = 1200
  values = concat([for filename in var.config.serving.values_files : file(filename)], [yamlencode({
    inferenceDomain = var.config.serving.inference_domain
    ingressClass    = var.config.ingress_class
    tlsSecretName   = var.config.serving.tls_secret_name
    servingVersion  = var.config.serving.version
  })])
  depends_on = [helm_release.serving_operator, helm_release.ingress]
}
