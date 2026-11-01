locals {
  serving = var.config.serving != null
  ingress_settings = {
    "controller.ingressClass"              = var.config.ingress_class
    "controller.ingressClassResource.name" = var.config.ingress_class
    "controller.service.type"              = "LoadBalancer"
  }
  prometheus_settings = {
    "stack.prometheus.enabled"       = false
    "stack.grafana.enabled"          = false
    "stack.alertmanager.enabled"     = false
    "stack.nodeExporter.enabled"     = false
    "stack.kubeStateMetrics.enabled" = false
    "stack.defaultRules.create"      = false
    "blackboxExporter.replicaCount"  = 0
  }
  worker_settings = {
    "control-plane.enabled"                                                      = false
    "runai-cluster.enabled"                                                      = true
    "runai-cluster.controlPlane.url"                                             = var.config.control_plane_url
    "runai-cluster.controlPlane.existingSecret"                                  = kubernetes_secret_v1.bootstrap.metadata[0].name
    "runai-cluster.controlPlane.secretKeys.clientSecret"                         = "bootstrap-token"
    "runai-cluster.controlPlane.bootstrapToken"                                  = "@/runai/config/client_secret"
    "runai-cluster.cluster.uid"                                                  = var.config.cluster_uid
    "runai-cluster.cluster.url"                                                  = "https://${var.config.domain}"
    "runai-cluster.clusterConfig.global.ingress.ingressClass"                    = var.config.ingress_class
    "runai-cluster.kai-resource-management.kai-scheduler.global.nvFractions.set" = false
  }
}

resource "nebius_applications_v1alpha1_k8s_release" "ingress" {
  count            = var.config.install_ingress ? 1 : 0
  parent_id        = var.parent_id
  cluster_id       = var.cluster_id
  application_name = "runai-ingress"
  namespace        = "ingress-nginx"
  product_slug     = "nebius/nginx-ingress-controller"
  sensitive = {
    set     = local.ingress_settings
    values  = var.config.ingress_values
    version = sha256(jsonencode([local.ingress_settings, var.config.ingress_values]))
  }
}

resource "nebius_applications_v1alpha1_k8s_release" "prometheus_operator" {
  count            = var.config.install_prometheus_operator ? 1 : 0
  parent_id        = var.parent_id
  cluster_id       = var.cluster_id
  application_name = "runai-prometheus-operator"
  namespace        = "monitoring"
  product_slug     = "bitnami/kube-prometheus"
  sensitive = {
    set     = local.prometheus_settings
    values  = var.config.prometheus_values
    version = sha256(jsonencode([local.prometheus_settings, var.config.prometheus_values]))
  }
}

resource "kubernetes_secret_v1" "bootstrap" {
  metadata {
    name      = "runai-bootstrap"
    namespace = "runai"
  }
  data_wo          = { "bootstrap-token" = var.bootstrap_token }
  data_wo_revision = var.config.bootstrap_token_revision
}

resource "nebius_applications_v1alpha1_k8s_release" "this" {
  parent_id        = var.parent_id
  cluster_id       = var.cluster_id
  application_name = "runai-cluster"
  namespace        = "runai"
  product_slug     = "nebius/nvidia-run-ai"
  sensitive = {
    set = merge(local.worker_settings, local.serving ? {
      "runai-cluster.clusterConfig.pod-grouper.args.gangScheduleKnative" = false
    } : {})
    values  = var.config.values
    version = sha256(jsonencode([local.worker_settings, var.config]))
  }
  depends_on = [nebius_applications_v1alpha1_k8s_release.ingress, nebius_applications_v1alpha1_k8s_release.prometheus_operator, kubectl_manifest.serving, kubectl_manifest.serving_ingress]
}

data "kubernetes_resources" "worker" {
  api_version    = "apps/v1"
  kind           = "Deployment"
  namespace      = "runai"
  field_selector = "metadata.name=runai-operator"
  depends_on     = [nebius_applications_v1alpha1_k8s_release.this]

  lifecycle {
    postcondition {
      condition = anytrue([for deployment in self.objects : try(
        deployment.spec.selector.matchLabels.name == "runai" &&
        deployment.spec.template.metadata.labels.app == "runai-operator" &&
        contains([for container in deployment.spec.template.spec.containers : container.name], "runai-go-operator"),
        false
      )])
      error_message = "The stock Run:ai worker Deployment was not installed. Publish/use Marketplace wrapper 0.3.2 or later; application DEPLOYED alone does not verify a worker."
    }
  }
}
