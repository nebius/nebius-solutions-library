resource "random_id" "oauth2_cookie" {
  byte_length = 16
}

resource "random_id" "mek_key" {
  byte_length = 32
}

resource "tls_private_key" "osmo_ingress" {
  count = var.tls_enabled && var.tls_mode == "self-signed" ? 1 : 0

  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_self_signed_cert" "osmo_ingress" {
  count = var.tls_enabled && var.tls_mode == "self-signed" ? 1 : 0

  private_key_pem       = tls_private_key.osmo_ingress[0].private_key_pem
  validity_period_hours = 8760
  early_renewal_hours   = 168
  dns_names             = [var.ingress_hostname]

  subject {
    common_name  = var.ingress_hostname
    organization = "OSMO tf-deploy"
  }

  allowed_uses = [
    "digital_signature",
    "key_encipherment",
    "server_auth",
  ]
}

resource "tls_private_key" "keycloak_ingress" {
  count = var.tls_enabled && var.tls_mode == "self-signed" ? 1 : 0

  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_self_signed_cert" "keycloak_ingress" {
  count = var.tls_enabled && var.tls_mode == "self-signed" ? 1 : 0

  private_key_pem       = tls_private_key.keycloak_ingress[0].private_key_pem
  validity_period_hours = 8760
  early_renewal_hours   = 168
  dns_names             = [local.auth_domain]

  subject {
    common_name  = local.auth_domain
    organization = "OSMO tf-deploy"
  }

  allowed_uses = [
    "digital_signature",
    "key_encipherment",
    "server_auth",
  ]
}

resource "kubernetes_secret_v1" "osmo_ingress_tls" {
  count = var.tls_enabled && var.tls_mode == "self-signed" ? 1 : 0

  metadata {
    name      = var.tls_secret_name
    namespace = kubernetes_namespace_v1.osmo.metadata[0].name
  }

  data = {
    "tls.crt" = tls_self_signed_cert.osmo_ingress[0].cert_pem
    "tls.key" = tls_private_key.osmo_ingress[0].private_key_pem
  }

  type = "kubernetes.io/tls"
}

resource "kubernetes_secret_v1" "keycloak_ingress_tls" {
  count = var.tls_enabled && var.tls_mode == "self-signed" ? 1 : 0

  metadata {
    name      = var.keycloak_tls_secret_name
    namespace = kubernetes_namespace_v1.osmo.metadata[0].name
  }

  data = {
    "tls.crt" = tls_self_signed_cert.keycloak_ingress[0].cert_pem
    "tls.key" = tls_private_key.keycloak_ingress[0].private_key_pem
  }

  type = "kubernetes.io/tls"
}

resource "terraform_data" "runtime_secrets" {
  input = {
    namespace                   = var.namespace
    postgres_host               = local.postgres_host
    storage_access_key_id       = local.storage_access_key_id
    storage_endpoint            = local.storage_endpoint
    postgresql_secret_id        = try(local.infra_mysterybox_secrets.postgresql_secret_id, "")
    storage_secret_reference_id = try(local.infra_storage_secret_reference_id, "")
  }

  triggers_replace = {
    namespace                   = var.namespace
    postgres_host               = local.postgres_host
    postgres_password_hash      = sha256(local.postgres_password)
    storage_access_key_id       = local.storage_access_key_id
    storage_secret_hash         = sha256(local.storage_secret_access_key)
    storage_endpoint            = local.storage_endpoint
    postgresql_secret_id        = try(local.infra_mysterybox_secrets.postgresql_secret_id, "")
    storage_secret_reference_id = try(local.infra_storage_secret_reference_id, "")
  }

  depends_on = [
    kubernetes_namespace_v1.osmo,
  ]

  provisioner "local-exec" {
    command = "/bin/bash ${path.module}/../scripts/apply-runtime-secrets.sh"

    environment = {
      KUBECONFIG                = pathexpand(var.kubeconfig_path)
      KUBECTL_CONTEXT           = var.kubeconfig_context != null ? var.kubeconfig_context : ""
      NAMESPACE                 = var.namespace
      POSTGRES_PASSWORD         = local.postgres_password
      STORAGE_ACCESS_KEY_ID     = local.storage_access_key_id
      STORAGE_SECRET_ACCESS_KEY = local.storage_secret_access_key
    }
  }
}

resource "kubernetes_config_map_v1" "mek_config" {
  metadata {
    name      = "mek-config"
    namespace = kubernetes_namespace_v1.osmo.metadata[0].name
  }

  data = {
    "mek.yaml" = <<-EOF
      currentMek: ${var.mek_id}
      meks:
        ${var.mek_id}: ${local.mek_encoded_value}
    EOF
  }
}

resource "kubernetes_secret_v1" "vault_secrets" {
  metadata {
    name      = "vault-secrets"
    namespace = kubernetes_namespace_v1.osmo.metadata[0].name
  }

  data = {
    "currentMek"         = var.mek_id
    "vault-secrets.yaml" = <<-EOF
      currentMek: ${var.mek_id}
      meks:
        ${var.mek_id}: ${local.mek_encoded_value}
    EOF
  }

  type = "Opaque"
}

resource "kubernetes_secret_v1" "oidc_secrets" {
  count = var.enable_auth ? 1 : 0

  metadata {
    name      = "oidc-secrets"
    namespace = kubernetes_namespace_v1.osmo.metadata[0].name
  }

  data = {
    "client_secret" = local.oidc_client_secret
  }

  type = "Opaque"
}

resource "kubernetes_secret_v1" "oauth2_proxy_secrets" {
  count = var.enable_auth ? 1 : 0

  metadata {
    name      = "oauth2-proxy-secrets"
    namespace = kubernetes_namespace_v1.osmo.metadata[0].name
  }

  data = {
    "client_secret" = local.oidc_client_secret
    "cookie_secret" = local.cookie_secret_value
  }

  type = "Opaque"
}

resource "kubernetes_secret_v1" "backend_operator_password" {
  count = var.deploy_backend_operator && local.backend_operator_login_method_effective == "password" ? 1 : 0

  metadata {
    name      = var.backend_operator_password_secret_name
    namespace = kubernetes_namespace_v1.backend_operator[0].metadata[0].name
  }

  data = {
    (var.backend_operator_password_secret_key) = var.backend_operator_password
  }

  type = "Opaque"
}

resource "kubernetes_secret_v1" "backend_operator_keycloak_ca" {
  count = local.backend_operator_self_signed_cert_enabled ? 1 : 0

  metadata {
    name      = local.backend_operator_keycloak_ca_secret_name
    namespace = kubernetes_namespace_v1.backend_operator[0].metadata[0].name
  }

  data = {
    "ca.crt" = tls_self_signed_cert.keycloak_ingress[0].cert_pem
  }

  type = "Opaque"
}

resource "terraform_data" "backend_operator_token_secret" {
  count = var.deploy_backend_operator && local.backend_operator_login_method_effective == "token" ? 1 : 0

  input = {
    namespace                      = var.namespace
    backend_namespace              = var.backend_operator_namespace
    enable_auth                    = tostring(var.enable_auth)
    keycloak_hostname              = local.auth_domain
    backend_operator_username      = var.backend_operator_username
    backend_operator_password_hash = sha256(var.backend_operator_password)
    skip_tls_verify                = tostring(var.tls_enabled && var.tls_mode == "self-signed")
    provided_token_hash            = sha256(var.backend_operator_service_token != null ? var.backend_operator_service_token : "")
    service_release_status         = try(helm_release.osmo_service.status, "")
  }

  triggers_replace = {
    namespace                      = var.namespace
    backend_namespace              = var.backend_operator_namespace
    enable_auth                    = tostring(var.enable_auth)
    keycloak_hostname              = local.auth_domain
    backend_operator_username      = var.backend_operator_username
    backend_operator_password_hash = sha256(var.backend_operator_password)
    skip_tls_verify                = tostring(var.tls_enabled && var.tls_mode == "self-signed")
    provided_token_hash            = sha256(var.backend_operator_service_token != null ? var.backend_operator_service_token : "")
    service_release_status         = try(helm_release.osmo_service.status, "")
  }

  depends_on = [
    kubernetes_namespace_v1.backend_operator,
    helm_release.osmo_service,
  ]

  provisioner "local-exec" {
    command = "/bin/bash ${path.module}/../scripts/prepare-backend-token.sh"

    environment = {
      KUBECONFIG                       = pathexpand(var.kubeconfig_path)
      KUBECTL_CONTEXT                  = var.kubeconfig_context != null ? var.kubeconfig_context : ""
      OSMO_NAMESPACE                   = var.namespace
      BACKEND_OPERATOR_NAMESPACE       = var.backend_operator_namespace
      ENABLE_AUTH                      = tostring(var.enable_auth)
      KEYCLOAK_HOSTNAME                = local.auth_domain
      BACKEND_OPERATOR_USERNAME        = var.backend_operator_username
      BACKEND_OPERATOR_PASSWORD        = var.backend_operator_password
      BACKEND_OPERATOR_SKIP_TLS_VERIFY = var.tls_enabled && var.tls_mode == "self-signed" ? "true" : "false"
      BACKEND_OPERATOR_SERVICE_TOKEN   = var.backend_operator_service_token != null ? var.backend_operator_service_token : ""
    }
  }
}
