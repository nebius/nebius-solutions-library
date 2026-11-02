locals {
  control         = var.config.role == "control-plane"
  worker_install  = !local.control && var.config.cluster_uid != ""
  public_endpoint = coalesce(var.config.public_endpoint, local.control)
  serving         = !local.control && var.config.serving != null
  namespace       = local.control ? "runai-backend" : "runai"
  resource_name   = "runai-${local.control ? "control" : "worker"}-${substr(sha256(var.cluster_id), 0, 10)}"
  domain          = local.public_endpoint ? "runai-${substr(split("-", nebius_tunnel_v1_tunnel.this[0].id)[1], 3, -1)}.tunnel.applications.${var.region}.nebius.cloud" : var.config.domain
  ingress_settings = merge({
    "controller.ingressClass"              = var.config.ingress_class
    "controller.ingressClassResource.name" = var.config.ingress_class
    "controller.service.type"              = local.public_endpoint ? "ClusterIP" : "LoadBalancer"
    }, local.public_endpoint ? {
    "fullnameOverride"                        = "runai-ingress"
    "controller.config.use-forwarded-headers" = true
  } : {})
  prometheus_settings = merge({
    "stack.prometheus.enabled"       = false
    "stack.grafana.enabled"          = false
    "stack.alertmanager.enabled"     = false
    "stack.nodeExporter.enabled"     = false
    "stack.kubeStateMetrics.enabled" = false
    "stack.defaultRules.create"      = false
    "blackboxExporter.replicas"      = 0
    }, { for component in ["kubelet", "kubeApiServer", "kubeControllerManager", "kubeScheduler", "kubeEtcd", "kubeProxy", "coreDns"] :
    "stack.${component}.enabled" => false
  })
  worker_values = {
    controlPlane = {
      url            = var.config.control_plane_url
      existingSecret = "runai-bootstrap"
      secretKeys     = { clientSecret = "bootstrap-token" }
      bootstrapToken = "@/runai/config/client_secret"
    }
    cluster = { uid = var.config.cluster_uid, url = "https://${local.domain}" }
    clusterConfig = merge({ global = { ingress = { ingressClass = var.config.ingress_class }, inference = { enabled = local.serving } } }, local.public_endpoint ? {
      "cluster-api" = { ingress = {
        tlsEnabled  = false
        annotations = { "nginx.ingress.kubernetes.io/ssl-redirect" = "false" }
      } }
    } : {}, local.serving ? { "pod-grouper" = { args = { gangScheduleKnative = false } } } : {})
    operands                  = { trainingOperator = { enabled = false } }
    "kai-resource-management" = { "kai-scheduler" = { global = { nvFractions = { set = false } } } }
  }
}

resource "nebius_applications_v1alpha1_k8s_release" "ingress" {
  count            = var.config.install_ingress && (!local.control || !local.public_endpoint) ? 1 : 0
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
  count            = !local.control && var.config.install_prometheus_operator ? 1 : 0
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
  count = local.worker_install ? 1 : 0
  metadata {
    name      = "runai-bootstrap"
    namespace = "runai"
  }
  data_wo          = { "bootstrap-token" = var.bootstrap_token }
  data_wo_revision = var.config.bootstrap_token_revision
}

resource "helm_release" "worker" {
  count         = local.worker_install ? 1 : 0
  name          = "runai-cluster"
  namespace     = "runai"
  repository    = "https://helm.ngc.nvidia.com/nvidia/runai-oss"
  chart         = "runai-cluster"
  version       = "3.0.17"
  atomic        = true
  wait_for_jobs = true
  timeout       = 1200
  values        = concat(var.config.values == null ? [] : [var.config.values], [yamlencode(local.worker_values)])
  depends_on    = [kubernetes_secret_v1.bootstrap, nebius_applications_v1alpha1_k8s_release.ingress, nebius_applications_v1alpha1_k8s_release.prometheus_operator, helm_release.tunnel_agent, kubectl_manifest.serving, kubectl_manifest.serving_ingress]
}

output "url" {
  description = "Control-plane or regional worker HTTPS URL; register this exact worker URL through Run:ai."
  value       = "https://${local.domain}"
}
