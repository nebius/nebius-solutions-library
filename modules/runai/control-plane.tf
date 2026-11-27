resource "nebius_msp_postgresql_v1alpha1_cluster" "this" {
  count      = local.control ? 1 : 0
  parent_id  = var.parent_id
  name       = local.resource_name
  network_id = var.network_id
  config = {
    version              = "16"
    public_access        = false
    postgresql_config_16 = {}
    pooler_config        = { pooling_mode = "SESSION", max_pool_size = 100 }
    template = {
      resources = { platform = var.config.postgresql.platform, preset = var.config.postgresql.preset }
      hosts     = { count = var.config.postgresql.host_count }
      disk      = { size_gibibytes = var.config.postgresql.disk_size_gib, type = "network-ssd" }
    }
  }
  bootstrap = { db_name = "runai", user_name = "runai" }
  sensitive = {
    bootstrap = { user_password = var.database_password }
    version   = tostring(var.config.credentials_revision)
  }
  backup = {
    backup_window_start = "01:00:00"
    retention_policy    = "${var.config.postgresql.backup_retention_days}d"
  }
  lifecycle {
    precondition {
      condition     = var.network_id != null
      error_message = "A control plane requires the Kubernetes cluster's private network_id."
    }
  }
}

locals {
  control_settings = local.control ? {
    "control-plane.enabled"                              = true
    "control-plane.fullnameOverride"                     = "runai-backend"
    "control-plane.global.resourceNamePrefix"            = "runai-backend"
    "marketplaceNaming.mode"                             = "legacy"
    "marketplaceAccess.mode"                             = local.public_endpoint ? "nebius" : "custom"
    "marketplaceAccess.fqdn"                             = local.public_endpoint ? "" : local.domain
    "marketplaceAccess.canonicalDomain"                  = local.domain
    "control-plane.global.domain"                        = local.domain
    "control-plane.global.ingress.enabled"               = !local.public_endpoint
    "control-plane.global.ingress.ingressClass"          = var.config.ingress_class
    "control-plane.global.ingress.tlsSecretName"         = "runai-backend-tls"
    "control-plane.global.postgresql.auth.host"          = nebius_msp_postgresql_v1alpha1_cluster.this[0].status.connection_endpoints.private_read_write
    "control-plane.global.postgresql.auth.username"      = "runai"
    "control-plane.global.postgresql.auth.database"      = "runai"
    "control-plane.global.postgresql.auth.dbSslMode"     = "require"
    "externalPostgresqlCredentials.create"               = true
    "adminCredentials.create"                            = true
    "control-plane.tenantsManager.config.adminUsername"  = var.config.admin_email
    "control-plane.tenantsManager.config.existingSecret" = "runai-admin-credentials"
    "tunna-agent.enabled"                                = local.public_endpoint
  } : {}
}

resource "nebius_applications_v1alpha1_k8s_release" "control_plane" {
  count            = local.control ? 1 : 0
  parent_id        = var.parent_id
  cluster_id       = var.cluster_id
  application_name = "runai-backend"
  namespace        = "runai-backend"
  product_slug     = "nebius/nvidia-run-ai"
  sensitive = {
    set = local.control_settings
    values = yamlencode(merge(yamldecode(coalesce(var.config.values, "{}")), {
      adminCredentials              = { password = var.admin_password }
      externalPostgresqlCredentials = { password = var.database_password, postgresPassword = var.database_password }
    }, local.public_endpoint ? { "tunna-agent" = local.tunnel_values } : {}))
    version = sha256(jsonencode([local.control_settings, local.tunnel_values, var.config.values, var.config.credentials_revision]))
  }
  depends_on = [nebius_iam_v1_group_membership.tunnel, nebius_iam_v1_access_permit.tunnel, nebius_applications_v1alpha1_k8s_release.ingress]
}
