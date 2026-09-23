locals {
  oauth_secret_name_normalized   = var.oauth_secret_name != null ? trimspace(var.oauth_secret_name) : ""
  oauth_client_id_normalized     = var.oauth_client_id != null ? trimspace(var.oauth_client_id) : ""
  oauth_client_secret_normalized = var.oauth_client_secret != null ? trimspace(var.oauth_client_secret) : ""
  use_oauth_secret               = local.oauth_secret_name_normalized != ""

  operator_config = merge(
    {
      defaultTags = var.default_tags
    },
    var.operator_hostname == null ? {} : {
      hostname = var.operator_hostname
    }
  )

  helm_values = merge(
    local.use_oauth_secret ? {} : {
      oauth = {
        clientId     = local.oauth_client_id_normalized
        clientSecret = local.oauth_client_secret_normalized
      }
    },
    local.use_oauth_secret && local.oauth_secret_name_normalized != "operator-oauth" ? {
      oauthSecretVolume = {
        secret = {
          secretName = local.oauth_secret_name_normalized
        }
      }
    } : {},
    {
      operatorConfig = local.operator_config
    }
  )
}

resource "kubernetes_namespace_v1" "this" {
  count = var.create_namespace ? 1 : 0

  metadata {
    name = var.namespace
  }
}

resource "helm_release" "this" {
  depends_on = [kubernetes_namespace_v1.this]

  name             = var.operator_name
  repository       = "https://pkgs.tailscale.com/helmcharts"
  chart            = "tailscale-operator"
  version          = var.operator_version
  namespace        = var.namespace
  create_namespace = false
  values           = [yamlencode(local.helm_values)]

  lifecycle {
    precondition {
      condition = !(
        local.use_oauth_secret &&
        (
          local.oauth_client_id_normalized != "" ||
          local.oauth_client_secret_normalized != ""
        )
      )
      error_message = "Set either oauth_secret_name or inline oauth_client_id/oauth_client_secret, not both."
    }

    precondition {
      condition = local.use_oauth_secret || (
        local.oauth_client_id_normalized != "" &&
        local.oauth_client_secret_normalized != ""
      )
      error_message = "When oauth_secret_name is not set, both oauth_client_id and oauth_client_secret must be provided."
    }
  }
}

# Some Cilium deployments require this setting so Tailscale proxy traffic returns
# through tailscale0 instead of being short-circuited by socket-level load balancing.
resource "kubernetes_config_map_v1_data" "cilium_hostns_only" {
  count = var.enable_cilium_bpf_lb_sock_hostns_only ? 1 : 0

  metadata {
    name      = var.cilium_config_map_name
    namespace = var.cilium_namespace
  }

  data = {
    "bpf-lb-sock-hostns-only" = "true"
  }

  force = true
}

resource "kubernetes_annotations" "restart_cilium_agent" {
  count = var.enable_cilium_bpf_lb_sock_hostns_only && var.restart_cilium_after_config_change ? 1 : 0

  api_version = "apps/v1"
  kind        = "DaemonSet"

  metadata {
    name      = var.cilium_daemonset_name
    namespace = var.cilium_namespace
  }

  template_annotations = {
    "tailscale.nebius.ai/cilium-config-restarted-for" = md5(jsonencode(kubernetes_config_map_v1_data.cilium_hostns_only[0].data))
  }

  depends_on = [
    kubernetes_config_map_v1_data.cilium_hostns_only
  ]
}
