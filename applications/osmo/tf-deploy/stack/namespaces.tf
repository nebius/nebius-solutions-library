resource "kubernetes_namespace_v1" "ingress" {
  count = var.deploy_ingress_nginx ? 1 : 0

  metadata {
    name = var.ingress_namespace
  }
}

resource "kubernetes_namespace_v1" "cert_manager" {
  count = var.tls_enabled && var.tls_mode == "cert-manager" && var.deploy_cert_manager ? 1 : 0

  metadata {
    name = var.cert_manager_namespace
  }
}

resource "kubernetes_namespace_v1" "osmo" {
  metadata {
    name = var.namespace
  }
}

resource "kubernetes_namespace_v1" "monitoring" {
  count = var.deploy_observability ? 1 : 0

  metadata {
    name = var.monitoring_namespace
  }
}

resource "kubernetes_namespace_v1" "backend_operator" {
  count = var.deploy_backend_operator ? 1 : 0

  metadata {
    name = var.backend_operator_namespace
  }
}

resource "kubernetes_namespace_v1" "workflows" {
  count = var.deploy_backend_operator ? 1 : 0

  metadata {
    name = var.workflows_namespace
  }
}

resource "kubernetes_namespace_v1" "gpu_operator" {
  count = local.deploy_gpu_infrastructure_effective ? 1 : 0

  metadata {
    name = var.gpu_operator_namespace
  }
}

resource "kubernetes_namespace_v1" "network_operator" {
  count = local.deploy_gpu_infrastructure_effective && var.deploy_network_operator ? 1 : 0

  metadata {
    name = var.network_operator_namespace
  }
}

resource "kubernetes_namespace_v1" "kai_scheduler" {
  count = local.deploy_gpu_infrastructure_effective ? 1 : 0

  metadata {
    name = var.kai_scheduler_namespace
  }
}
