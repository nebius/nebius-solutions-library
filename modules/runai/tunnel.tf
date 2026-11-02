resource "nebius_tunnel_v1_tunnel" "this" {
  count     = local.public_endpoint ? 1 : 0
  parent_id = var.parent_id
  name      = local.resource_name
  title     = local.resource_name
}

resource "nebius_iam_v1_service_account" "tunnel" {
  count     = local.public_endpoint ? 1 : 0
  parent_id = var.parent_id
  name      = local.resource_name
}

resource "nebius_iam_v1_group" "tunnel" {
  count     = local.public_endpoint ? 1 : 0
  parent_id = var.parent_id
  name      = "${local.resource_name}-tunnel-agents"
}

resource "nebius_iam_v1_group_membership" "tunnel" {
  count     = local.public_endpoint ? 1 : 0
  parent_id = nebius_iam_v1_group.tunnel[0].id
  member_id = nebius_iam_v1_service_account.tunnel[0].id
}

resource "nebius_iam_v1_access_permit" "tunnel" {
  count       = local.public_endpoint ? 1 : 0
  parent_id   = nebius_iam_v1_group.tunnel[0].id
  resource_id = nebius_tunnel_v1_tunnel.this[0].id
  role        = "applicationtunnel.agent"
}

resource "nebius_iam_v1_auth_public_key" "tunnel" {
  count     = local.public_endpoint ? 1 : 0
  parent_id = var.parent_id
  name      = local.resource_name
  account   = { service_account = { id = nebius_iam_v1_service_account.tunnel[0].id } }
  data      = file(var.config.tunnel_public_key_file)
}

resource "kubernetes_secret_v1" "tunnel" {
  count = local.public_endpoint ? 1 : 0
  metadata {
    name      = "runai-tunnel-key"
    namespace = local.namespace
  }
  data_wo          = { "private-key.pem" = var.tunnel_private_key_pem }
  data_wo_revision = var.config.credentials_revision
}

locals {
  tunnel_values = local.public_endpoint ? {
    config = {
      server_addr = "tunnel.applications.${var.region}.nebius.cloud:8443"
      tunnel_id   = nebius_tunnel_v1_tunnel.this[0].id
      iam = {
        domain = "api.nebius.cloud"
        service_account = {
          service_account_id = nebius_iam_v1_service_account.tunnel[0].id
          public_key_id      = nebius_iam_v1_auth_public_key.tunnel[0].id
          private_key_path   = "/run/tunnel-identity/private_key.pem"
        }
      }
      services = [{
        name   = "runai"
        target = local.control ? "runai-backend-traefik:8080" : var.config.tunnel_target
        type   = "http"
      }]
    }
    secret = { data = {}, items = [] }
    volumes = [{
      name = "tunnel-identity"
      secret = {
        secretName = kubernetes_secret_v1.tunnel[0].metadata[0].name
        items      = [{ key = "private-key.pem", path = "private_key.pem" }]
      }
    }]
    volumeMounts = [{ name = "tunnel-identity", mountPath = "/run/tunnel-identity", readOnly = true }]
  } : null
}

resource "helm_release" "tunnel_agent" {
  count         = !local.control && local.public_endpoint ? 1 : 0
  name          = "runai-tunnel"
  namespace     = "runai"
  repository    = "oci://cr.eu-north1.nebius.cloud/marketplace/chart"
  chart         = "nebius-tunnel-agent"
  version       = "1.2.0"
  atomic        = true
  wait_for_jobs = true
  timeout       = 600
  values        = [yamlencode(local.tunnel_values)]
  depends_on    = [nebius_iam_v1_group_membership.tunnel, nebius_iam_v1_access_permit.tunnel, nebius_applications_v1alpha1_k8s_release.ingress]
}
