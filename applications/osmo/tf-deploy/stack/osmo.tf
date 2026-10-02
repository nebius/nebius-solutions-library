resource "helm_release" "redis" {
  name       = "redis"
  namespace  = kubernetes_namespace_v1.osmo.metadata[0].name
  repository = "https://charts.bitnami.com/bitnami"
  chart      = "redis"
  version    = var.redis_chart_version

  set = [
    {
      name  = "architecture"
      value = "standalone"
    },
    {
      name  = "auth.enabled"
      value = "false"
    },
    {
      name  = "networkPolicy.enabled"
      value = "false"
    },
    {
      name  = "master.persistence.size"
      value = "50Gi"
    },
    {
      name  = "master.resources.requests.cpu"
      value = "8"
    },
    {
      name  = "master.resources.requests.memory"
      value = "52820Mi"
    },
    {
      name  = "master.resources.limits.cpu"
      value = "8"
    },
    {
      name  = "master.resources.limits.memory"
      value = "52820Mi"
    },
    {
      name  = "commonConfiguration"
      value = "aof-load-corrupt-tail-max-size 10000000"
    }
  ]

  depends_on = [
    kubernetes_namespace_v1.osmo,
  ]
}

resource "helm_release" "osmo_service" {
  name            = "osmo-service"
  namespace       = kubernetes_namespace_v1.osmo.metadata[0].name
  repository      = "https://helm.ngc.nvidia.com/nvidia/osmo"
  chart           = "service"
  version         = var.osmo_chart_version
  values          = [yamlencode(local.osmo_service_values)]
  atomic          = true
  cleanup_on_fail = true
  timeout         = 1800

  depends_on = [
    terraform_data.validate,
    terraform_data.ingress_ready,
    terraform_data.keycloak_bootstrap,
    terraform_data.runtime_secrets,
    terraform_data.cert_manager_cluster_issuer,
    kubernetes_secret_v1.osmo_ingress_tls,
    kubernetes_config_map_v1.mek_config,
    kubernetes_secret_v1.vault_secrets,
    kubernetes_secret_v1.oidc_secrets,
    kubernetes_secret_v1.oauth2_proxy_secrets,
    helm_release.redis,
  ]
}

resource "helm_release" "osmo_router" {
  name            = "osmo-router"
  namespace       = kubernetes_namespace_v1.osmo.metadata[0].name
  repository      = "https://helm.ngc.nvidia.com/nvidia/osmo"
  chart           = "router"
  version         = var.osmo_chart_version
  values          = [yamlencode(local.router_values)]
  atomic          = true
  cleanup_on_fail = true
  timeout         = 1200

  depends_on = [
    terraform_data.ingress_ready,
    helm_release.osmo_service,
    terraform_data.cert_manager_cluster_issuer,
    kubernetes_secret_v1.osmo_ingress_tls,
    kubernetes_config_map_v1.mek_config,
    terraform_data.runtime_secrets,
  ]
}

resource "helm_release" "osmo_ui" {
  count           = var.deploy_ui ? 1 : 0
  name            = "osmo-ui"
  namespace       = kubernetes_namespace_v1.osmo.metadata[0].name
  repository      = "https://helm.ngc.nvidia.com/nvidia/osmo"
  chart           = "web-ui"
  version         = var.osmo_chart_version
  values          = [yamlencode(local.ui_values)]
  atomic          = true
  cleanup_on_fail = true
  timeout         = 1200

  depends_on = [
    terraform_data.ingress_ready,
    helm_release.osmo_service,
    helm_release.osmo_router,
    terraform_data.cert_manager_cluster_issuer,
    kubernetes_secret_v1.osmo_ingress_tls,
  ]
}
