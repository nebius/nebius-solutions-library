locals {
  keycloak_values = {
    global = {
      security = {
        allowInsecureImages = true
      }
    }
    image = {
      registry   = "docker.io"
      repository = "bitnamilegacy/keycloak"
      tag        = "26.1.1-debian-12-r0"
    }
    production   = true
    proxy        = "edge"
    proxyHeaders = "xforwarded"
    hostname     = local.auth_domain
    auth = {
      adminUser     = "admin"
      adminPassword = local.keycloak_admin_password
    }
    ingress = {
      enabled          = true
      tls              = false
      ingressClassName = "nginx"
      hostname         = local.auth_domain
      annotations = merge(
        {
          "nginx.ingress.kubernetes.io/proxy-buffer-size" = "16k"
        },
        var.tls_enabled ? {
          "nginx.ingress.kubernetes.io/ssl-redirect" = "true"
        } : {},
        var.tls_enabled && var.tls_mode == "cert-manager" ? {
          "cert-manager.io/cluster-issuer" = var.cluster_issuer_name
        } : {}
      )
      path        = "/"
      pathType    = "Prefix"
      servicePort = "http"
      extraTls = var.tls_enabled ? [
        {
          hosts      = [local.auth_domain]
          secretName = var.keycloak_tls_secret_name
        }
      ] : []
    }
    replicaCount = 1
    resources = {
      requests = {
        cpu    = "500m"
        memory = "768Mi"
      }
      limits = {
        cpu    = "2"
        memory = "1Gi"
      }
    }
    postgresql = {
      enabled = true
      image = {
        registry   = "docker.io"
        repository = "bitnamilegacy/postgresql"
        tag        = "17.6.0-debian-12-r4"
      }
      auth = {
        username = "keycloak"
        password = local.keycloak_db_password
        database = "keycloak"
      }
    }
    extraEnvVars = [
      {
        name  = "KC_HTTP_ENABLED"
        value = "true"
      },
      {
        name  = "KC_HEALTH_ENABLED"
        value = "true"
      },
      {
        name  = "KC_HOSTNAME"
        value = local.auth_domain
      },
      {
        name  = "KC_HOSTNAME_STRICT"
        value = "true"
      },
      {
        name  = "KC_HOSTNAME_STRICT_HTTPS"
        value = "true"
      },
      {
        name  = "KC_PROXY"
        value = "edge"
      }
    ]
  }
}

resource "terraform_data" "cleanup_stale_keycloak_release" {
  count = var.enable_auth ? 1 : 0

  input = {
    namespace    = var.namespace
    release_name = var.keycloak_release_name
  }

  triggers_replace = {
    namespace    = var.namespace
    release_name = var.keycloak_release_name
  }

  depends_on = [
    kubernetes_namespace_v1.osmo,
  ]

  provisioner "local-exec" {
    command = "/bin/bash ${path.module}/../scripts/cleanup-stale-helm-release.sh"

    environment = {
      KUBECONFIG      = pathexpand(var.kubeconfig_path)
      KUBECTL_CONTEXT = var.kubeconfig_context != null ? var.kubeconfig_context : ""
      NAMESPACE       = var.namespace
      RELEASE_NAME    = var.keycloak_release_name
    }
  }
}

resource "helm_release" "keycloak" {
  count = var.enable_auth ? 1 : 0

  name            = var.keycloak_release_name
  namespace       = kubernetes_namespace_v1.osmo.metadata[0].name
  repository      = "https://charts.bitnami.com/bitnami"
  chart           = "keycloak"
  version         = var.keycloak_chart_version
  values          = [yamlencode(local.keycloak_values)]
  atomic          = true
  cleanup_on_fail = true
  timeout         = 1200

  depends_on = [
    terraform_data.cleanup_stale_keycloak_release,
    terraform_data.cert_manager_cluster_issuer,
    kubernetes_namespace_v1.osmo,
    kubernetes_secret_v1.keycloak_ingress_tls,
  ]
}

resource "terraform_data" "keycloak_bootstrap" {
  count = var.enable_auth ? 1 : 0

  input = {
    keycloak_hostname        = local.auth_domain
    osmo_hostname            = var.ingress_hostname
    oidc_client_secret_hash  = sha256(local.oidc_client_secret)
    keycloak_admin_hash      = sha256(local.keycloak_admin_password)
    nebius_sso_enabled       = tostring(var.nebius_sso_enabled)
    nebius_sso_client_id     = var.nebius_sso_client_id != null ? var.nebius_sso_client_id : ""
    nebius_sso_client_secret = sha256(var.nebius_sso_client_secret != null ? var.nebius_sso_client_secret : "")
    nebius_sso_issuer_url    = var.nebius_sso_issuer_url
    nebius_sso_group_attr    = var.nebius_sso_group_attribute
    breakglass_user_enabled  = tostring(var.keycloak_create_breakglass_user)
    keycloak_release_name    = var.keycloak_release_name
    keycloak_chart_version   = var.keycloak_chart_version
  }

  triggers_replace = {
    keycloak_hostname        = local.auth_domain
    osmo_hostname            = var.ingress_hostname
    oidc_client_secret_hash  = sha256(local.oidc_client_secret)
    keycloak_admin_hash      = sha256(local.keycloak_admin_password)
    nebius_sso_enabled       = tostring(var.nebius_sso_enabled)
    nebius_sso_client_id     = var.nebius_sso_client_id != null ? var.nebius_sso_client_id : ""
    nebius_sso_client_secret = sha256(var.nebius_sso_client_secret != null ? var.nebius_sso_client_secret : "")
    nebius_sso_issuer_url    = var.nebius_sso_issuer_url
    nebius_sso_group_attr    = var.nebius_sso_group_attribute
    breakglass_user_enabled  = tostring(var.keycloak_create_breakglass_user)
    keycloak_release_name    = var.keycloak_release_name
    keycloak_chart_version   = var.keycloak_chart_version
  }

  depends_on = [
    helm_release.keycloak,
  ]

  provisioner "local-exec" {
    command = "/bin/bash ${path.module}/../scripts/bootstrap-keycloak.sh"

    environment = {
      KUBECONFIG                 = pathexpand(var.kubeconfig_path)
      KUBECTL_CONTEXT            = var.kubeconfig_context != null ? var.kubeconfig_context : ""
      NAMESPACE                  = var.namespace
      RELEASE_NAME               = var.keycloak_release_name
      KEYCLOAK_HOSTNAME          = local.auth_domain
      OSMO_INGRESS_HOSTNAME      = var.ingress_hostname
      KEYCLOAK_ADMIN_PASSWORD    = local.keycloak_admin_password
      OIDC_CLIENT_SECRET         = local.oidc_client_secret
      NEBIUS_SSO_ENABLED         = tostring(var.nebius_sso_enabled)
      NEBIUS_SSO_ISSUER_URL      = var.nebius_sso_issuer_url
      NEBIUS_SSO_CLIENT_ID       = var.nebius_sso_client_id != null ? var.nebius_sso_client_id : ""
      NEBIUS_SSO_CLIENT_SECRET   = var.nebius_sso_client_secret != null ? var.nebius_sso_client_secret : ""
      NEBIUS_SSO_GROUP_ATTRIBUTE = var.nebius_sso_group_attribute
      CREATE_BREAKGLASS_USER     = tostring(var.keycloak_create_breakglass_user)
      REALM_TEMPLATE             = "${path.module}/../config/keycloak/realm.json"
    }
  }
}
