resource "kubernetes_namespace" "runai_backend" {
  metadata {
    name = "runai-backend"
  }

  depends_on = [
    nebius_mk8s_v1_cluster.k8s-cluster
  ]
}

################
# TLS
################
resource "tls_private_key" "runai" {
  count = var.tls_mode == "self-signed" ? 1 : 0

  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "tls_self_signed_cert" "runai" {
  count = var.tls_mode == "self-signed" ? 1 : 0

  private_key_pem       = tls_private_key.runai[0].private_key_pem
  validity_period_hours = 24 * 365
  early_renewal_hours   = 720
  allowed_uses          = ["server_auth", "digital_signature", "key_encipherment"]

  dns_names = [
    "runai.${local.runai_base_domain}",
    "*.runai.${local.runai_base_domain}",
  ]

  subject {
    common_name = "runai.${local.runai_base_domain}"
  }

  lifecycle {
    precondition {
      # Only meaningful in self-signed mode (the resource is count = 0 in byo
      # mode); the precondition never blocks destroy.
      condition     = var.tls_mode != "self-signed" || (local.haproxy_lb_ip != null && local.haproxy_lb_ip != "")
      error_message = "HAProxy LoadBalancer IP is not ready yet — re-run terraform apply once the ingress Service has an external IP (see README)."
    }
  }
}

locals {
  runai_tls_cert_pem = var.tls_mode == "byo" ? var.tls_certificate : tls_self_signed_cert.runai[0].cert_pem
  runai_tls_key_pem  = var.tls_mode == "byo" ? var.tls_private_key : tls_private_key.runai[0].private_key_pem
}

resource "kubernetes_secret" "runai_backend_tls" {
  metadata {
    name      = "runai-backend-tls"
    namespace = kubernetes_namespace.runai_backend.metadata[0].name
  }

  type = "kubernetes.io/tls"

  data = {
    "tls.crt" = local.runai_tls_cert_pem
    "tls.key" = local.runai_tls_key_pem
  }
}

################
# PULL SECRET
################
resource "kubernetes_secret" "runai_reg_creds" {
  metadata {
    name      = "runai-reg-creds"
    namespace = kubernetes_namespace.runai_backend.metadata[0].name
  }

  type = "kubernetes.io/dockerconfigjson"

  data = {
    ".dockerconfigjson" = jsonencode({
      auths = {
        "nvcr.io" = {
          username = "$oauthtoken"
          password = var.ngc_api_key
          auth     = base64encode("$oauthtoken:${var.ngc_api_key}")
        }
      }
    })
  }
}

################
# CONTROL PLANE
################
resource "helm_release" "runai_backend" {
  name                = "runai-backend"
  namespace           = kubernetes_namespace.runai_backend.metadata[0].name
  repository          = "https://helm.ngc.nvidia.com/nvidia/runai"
  chart               = "control-plane"
  version             = var.runai_version
  repository_username = "$oauthtoken"
  repository_password = var.ngc_api_key
  wait                = true
  timeout             = var.helm_timeout
  cleanup_on_fail     = true

  values = [
    yamlencode({
      global = {
        # The chart uses global.domain verbatim as the UI Ingress host (verified
        # against control-plane 2.26.50: host == global.domain, KC_HOSTNAME ==
        # https://<global.domain>/auth), so it must be the full UI hostname —
        # matching the TLS cert SANs (runai.<base>, *.runai.<base>) and the
        # runai_ui_url output.
        domain = "runai.${local.runai_base_domain}"
        ingress = {
          ingressClass  = "haproxy"
          tlsSecretName = kubernetes_secret.runai_backend_tls.metadata[0].name
        }
      }
      tenantsManager = {
        config = {
          adminUsername = var.runai_admin_username
          adminPassword = var.runai_admin_password
        }
      }
    })
  ]

  depends_on = [
    helm_release.ingress_controller,
    kubernetes_secret.runai_backend_tls,
    kubernetes_secret.runai_reg_creds,
    module.gpu-operator
  ]

  lifecycle {
    precondition {
      # The helm values embed runai.<base domain>, which in self-signed mode is
      # derived from the HAProxy LB IP — never block byo installs on it. The
      # precondition never runs during destroy.
      condition     = var.tls_mode != "self-signed" || (local.haproxy_lb_ip != null && local.haproxy_lb_ip != "")
      error_message = "HAProxy LoadBalancer IP is not ready yet — re-run terraform apply once the ingress Service has an external IP (see README)."
    }
  }
}
