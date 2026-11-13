resource "helm_release" "this" {
  name             = "nim-operator"
  namespace        = var.config.namespace
  repository       = var.config.chart_path == null ? "https://helm.ngc.nvidia.com/nvidia" : null
  chart            = coalesce(var.config.chart_path, "k8s-nim-operator")
  version          = var.config.version
  create_namespace = true
  atomic           = true
  wait_for_jobs    = true
  timeout          = 1200
  # Dynamo/Grove/KAI have a separate owner; never install the chart's older Dynamo dependency.
  values = concat([for filename in var.config.values_files : file(filename)], [yamlencode({ dynamo = { enabled = false } })])
}

resource "kubernetes_cluster_role_v1" "runai_cache_reader" {
  count = var.config.runai_cache_reader == null ? 0 : 1
  metadata {
    name = "${var.config.namespace}-runai-cache-reader"
  }
  rule {
    api_groups = ["apps.nvidia.com"]
    resources  = ["nimcaches"]
    verbs      = ["get", "list", "watch"]
  }
}

resource "kubernetes_cluster_role_binding_v1" "runai_cache_reader" {
  count = var.config.runai_cache_reader == null ? 0 : 1
  metadata {
    name = kubernetes_cluster_role_v1.runai_cache_reader[0].metadata[0].name
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.runai_cache_reader[0].metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = var.config.runai_cache_reader.service_account
    namespace = var.config.runai_cache_reader.namespace
  }
}
