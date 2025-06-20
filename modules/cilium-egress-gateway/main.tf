resource "kubernetes_config_map_v1_data" "cilium-config" {
  metadata {
    name      = "cilium-config"
    namespace = "kube-system"
  }
  data = {
    "enable-ipv4-egress-gateway" = "true"
  }
}

resource "kubernetes_annotations" "restart_cilium_agent" {
  api_version = "apps/v1"
  kind        = "DaemonSet"
  metadata {
    name      = "cilium"
    namespace = "kube-system"
  }
  template_annotations = {
    "restarted_for" = "egress-gateway"
  }
  depends_on = [
    resource.kubernetes_config_map_v1_data.cilium-config
  ]
}

resource "kubernetes_annotations" "restart_cilium_operator" {
  api_version = "apps/v1"
  kind        = "Deployment"
  metadata {
    name      = "cilium-operator"
    namespace = "kube-system"
  }
  template_annotations = {
    "restarted_for" = "egress-gateway"
  }
  depends_on = [
    resource.kubernetes_config_map_v1_data.cilium-config
  ]
}