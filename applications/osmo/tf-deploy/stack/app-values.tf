locals {
  auth_domain                   = coalesce(var.keycloak_hostname, "auth-${var.ingress_hostname}")
  keycloak_external_url         = coalesce(var.keycloak_external_url, "https://${local.auth_domain}")
  service_base_url_value        = coalesce(var.service_base_url, "${var.tls_enabled ? "https" : "http"}://${var.ingress_hostname}")
  storage_endpoint_value        = trimsuffix(local.storage_endpoint, "/")
  cookie_secret_value           = coalesce(var.oauth2_proxy_cookie_secret, random_id.oauth2_cookie.hex)
  ingress_controller_cluster_ip = var.enable_auth ? try(data.kubernetes_service_v1.ingress_controller[0].spec[0].cluster_ip, "") : ""
  oauth2_proxy_insecure_skip_tls_verify_effective = (
    var.oauth2_proxy_insecure_skip_tls_verify != null
    ? var.oauth2_proxy_insecure_skip_tls_verify
    : (var.tls_enabled && var.tls_mode == "self-signed")
  )
  in_cluster_host_aliases = local.ingress_controller_cluster_ip != "" ? [
    {
      ip        = local.ingress_controller_cluster_ip
      hostnames = distinct(compact([local.auth_domain, var.ingress_hostname]))
    }
  ] : []
  oauth2_proxy_common_extra_args = concat(
    [
      "--insecure-oidc-allow-unverified-email=true",
      "--oidc-email-claim=preferred_username",
    ],
    local.oauth2_proxy_insecure_skip_tls_verify_effective ? ["--ssl-insecure-skip-verify=true"] : []
  )
  oauth2_proxy_common = {
    enabled              = var.enable_auth
    oidcIssuerUrl        = var.enable_auth ? "${local.keycloak_external_url}/realms/osmo" : ""
    clientId             = var.enable_auth ? "osmo-browser-flow" : ""
    cookieDomain         = var.enable_auth ? var.ingress_hostname : ""
    cookieSecure         = var.enable_auth
    cookieRefresh        = var.enable_auth ? var.oauth2_proxy_cookie_refresh : ""
    useKubernetesSecrets = var.enable_auth
    secretName           = var.enable_auth ? "oauth2-proxy-secrets" : ""
    clientSecretKey      = var.enable_auth ? "client_secret" : ""
    cookieSecretKey      = var.enable_auth ? "cookie_secret" : ""
    extraArgs            = var.enable_auth ? local.oauth2_proxy_common_extra_args : []
  }
  mek_encoded_value = coalesce(var.mek_encoded, base64encode(jsonencode({
    k   = random_id.mek_key.b64_std
    kid = var.mek_id
    kty = "oct"
  })))

  db_env = [
    {
      name  = "OSMO_POSTGRES_HOST"
      value = local.postgres_host
    },
    {
      name  = "OSMO_POSTGRES_PORT"
      value = tostring(local.postgres_port)
    },
    {
      name  = "OSMO_POSTGRES_USER"
      value = local.postgres_user
    },
    {
      name  = "OSMO_POSTGRES_DATABASE"
      value = local.postgres_db
    },
    {
      name = "OSMO_POSTGRES_PASSWORD"
      valueFrom = {
        secretKeyRef = {
          name = "db-secret"
          key  = "db-password"
        }
      }
    },
    {
      name  = "METRICS_OTEL_ENABLE"
      value = "false"
    }
  ]

  storage_env = [
    {
      name  = "AWS_ENDPOINT_URL_S3"
      value = local.storage_endpoint_value
    },
    {
      name  = "AWS_S3_FORCE_PATH_STYLE"
      value = "true"
    },
    {
      name  = "AWS_DEFAULT_REGION"
      value = local.storage_region
    },
    {
      name = "AWS_ACCESS_KEY_ID"
      valueFrom = {
        secretKeyRef = {
          name = "osmo-storage"
          key  = "access-key-id"
        }
      }
    },
    {
      name = "AWS_SECRET_ACCESS_KEY"
      valueFrom = {
        secretKeyRef = {
          name = "osmo-storage"
          key  = "secret-access-key"
        }
      }
    },
    {
      name  = "AWS_EC2_METADATA_DISABLED"
      value = "true"
    }
  ]

  vault_extra_volumes = [
    {
      name = "vault-secrets"
      secret = {
        secretName = "vault-secrets"
      }
    }
  ]

  vault_extra_mounts = [
    {
      name      = "vault-secrets"
      mountPath = "/home/osmo/vault-agent/secrets"
      readOnly  = true
    }
  ]

  service_ingress_annotations = merge(
    {
      "nginx.ingress.kubernetes.io/proxy-buffer-size"           = "16k"
      "nginx.ingress.kubernetes.io/proxy-buffers"               = "8 16k"
      "nginx.ingress.kubernetes.io/proxy-busy-buffers-size"     = "32k"
      "nginx.ingress.kubernetes.io/large-client-header-buffers" = "4 16k"
      "nginx.ingress.kubernetes.io/proxy-read-timeout"          = "300"
      "nginx.ingress.kubernetes.io/proxy-send-timeout"          = "300"
      "nginx.ingress.kubernetes.io/proxy-connect-timeout"       = "60"
    },
    var.tls_enabled ? {
      "nginx.ingress.kubernetes.io/ssl-redirect" = "true"
    } : {},
    var.tls_enabled && var.tls_mode == "cert-manager" ? {
      "cert-manager.io/cluster-issuer" = var.cluster_issuer_name
    } : {}
  )

  proxy_buffer_annotations = {
    "nginx.ingress.kubernetes.io/proxy-buffer-size"    = "16k"
    "nginx.ingress.kubernetes.io/proxy-buffers-number" = "4"
  }

  service_tls_block = var.tls_enabled ? [
    {
      hosts      = [var.ingress_hostname]
      secretName = var.tls_secret_name
    }
  ] : null

  ingress_nginx_values = {
    controller = merge(
      {
        progressDeadlineSeconds = 600
        service = merge(
          {
            type = "LoadBalancer"
          },
          length(var.ingress_service_annotations) > 0 ? {
            annotations = var.ingress_service_annotations
          } : {}
        )
      },
      {
        config = {
          "allow-snippet-annotations" = "true"
          "annotations-risk-level"    = "Critical"
        }
      }
    )
  }

  auth_block = {
    enabled           = true
    device_endpoint   = "${local.keycloak_external_url}/realms/osmo/protocol/openid-connect/auth/device"
    device_client_id  = "osmo-device"
    browser_endpoint  = "${local.keycloak_external_url}/realms/osmo/protocol/openid-connect/auth"
    browser_client_id = "osmo-browser-flow"
    token_endpoint    = "${local.keycloak_external_url}/realms/osmo/protocol/openid-connect/token"
    logout_endpoint   = "${local.keycloak_external_url}/realms/osmo/protocol/openid-connect/logout"
  }

  service_authz_extra_env = [
    {
      name = "OSMO_POSTGRES_PASSWORD"
      valueFrom = {
        secretKeyRef = {
          name = "db-secret"
          key  = "db-password"
        }
      }
    },
    {
      name = "PGPASSWORD"
      valueFrom = {
        secretKeyRef = {
          name = "db-secret"
          key  = "db-password"
        }
      }
    },
    {
      name = "POSTGRES_PASSWORD"
      valueFrom = {
        secretKeyRef = {
          name = "db-secret"
          key  = "db-password"
        }
      }
    }
  ]

  shared_oidc_providers = [
    {
      issuer     = "${local.keycloak_external_url}/realms/osmo"
      audience   = "osmo-device"
      jwks_uri   = "${local.keycloak_external_url}/realms/osmo/protocol/openid-connect/certs"
      user_claim = "preferred_username"
      cluster    = "idp"
    },
    {
      issuer     = "${local.keycloak_external_url}/realms/osmo"
      audience   = "osmo-browser-flow"
      jwks_uri   = "${local.keycloak_external_url}/realms/osmo/protocol/openid-connect/certs"
      user_claim = "preferred_username"
      cluster    = "idp"
    }
  ]

  service_envoy_providers = concat(local.shared_oidc_providers, [
    {
      issuer     = "osmo"
      audience   = "osmo"
      jwks_uri   = "http://localhost:8000/api/auth/keys"
      user_claim = "unique_name"
      cluster    = "service"
    }
  ])

  router_envoy_providers = concat(local.shared_oidc_providers, [
    {
      issuer     = "osmo"
      audience   = "osmo"
      jwks_uri   = "http://osmo-service/api/auth/keys"
      user_claim = "unique_name"
      cluster    = "osmoauth"
    }
  ])

  service_sidecars = {
    authz = {
      enabled  = var.enable_auth
      extraEnv = var.enable_auth ? local.service_authz_extra_env : []
    }
    envoy = {
      enabled = var.enable_auth
      idp = {
        host = local.auth_domain
      }
      skipAuthPaths = var.enable_auth ? [
        "/api/version",
        "/api/auth/login",
        "/api/auth/keys",
        "/api/auth/refresh_token",
        "/api/auth/jwt/refresh_token",
        "/api/auth/jwt/access_token",
        "/client/version"
      ] : []
      service = {
        port     = 8000
        hostname = var.ingress_hostname
        address  = "127.0.0.1"
      }
      jwt = {
        user_header = "x-osmo-user"
        providers   = var.enable_auth ? local.service_envoy_providers : []
      }
    }
    oauth2Proxy = {
      for k, v in local.oauth2_proxy_common : k => v
    }
    rateLimit = {
      enabled = false
    }
    logAgent = {
      enabled = false
    }
    otel = {
      enabled = false
    }
  }

  router_sidecars = {
    logAgent = {
      enabled = false
    }
    envoy = {
      enabled = var.enable_auth
      idp = {
        host = local.auth_domain
      }
      skipAuthPaths = var.enable_auth ? [
        "/api/router/version"
      ] : []
      service = {
        hostname = var.ingress_hostname
      }
      jwt = {
        enabled     = var.enable_auth
        user_header = "x-osmo-user"
        providers   = var.enable_auth ? local.router_envoy_providers : []
      }
      osmoauth = {
        enabled  = var.enable_auth
        port     = var.enable_auth ? 80 : 0
        hostname = var.enable_auth ? var.ingress_hostname : ""
        address  = var.enable_auth ? "osmo-service" : ""
      }
    }
    oauth2Proxy = {
      for k, v in local.oauth2_proxy_common : k => v
    }
  }

  ui_sidecars = {
    logAgent = {
      enabled = false
    }
    envoy = {
      enabled = var.enable_auth
      idp = {
        host = local.auth_domain
      }
      service = {
        hostname = var.ingress_hostname
        address  = "127.0.0.1"
        port     = 8000
      }
      jwt = {
        enabled     = var.enable_auth
        user_header = "x-osmo-user"
        providers   = var.enable_auth ? local.shared_oidc_providers : []
      }
    }
    oauth2Proxy = merge(
      local.oauth2_proxy_common,
      {
        oidcEndSessionUrl = var.enable_auth ? "${local.keycloak_external_url}/realms/osmo/protocol/openid-connect/logout" : ""
        extraArgs         = var.enable_auth ? concat(local.oauth2_proxy_common_extra_args, ["--whitelist-domain=${local.auth_domain}"]) : []
      }
    )
  }

  osmo_service_values = {
    global = {
      osmoImageLocation = "nvcr.io/nvidia/osmo"
      osmoImageTag      = var.osmo_image_tag
      imagePullPolicy   = "IfNotPresent"
    }

    podMonitor = {
      enabled = false
    }

    services = {
      postgres = {
        enabled            = false
        serviceName        = local.postgres_host
        port               = local.postgres_port
        db                 = local.postgres_db
        user               = local.postgres_user
        passwordSecretName = "db-secret"
        passwordSecretKey  = "db-password"
      }
      redis = {
        enabled     = false
        serviceName = "redis-master.${var.namespace}.svc.cluster.local"
        port        = 6379
        tlsEnabled  = false
      }
      service = {
        scaling = {
          minReplicas = 1
          maxReplicas = 1
        }
        hostname    = var.ingress_hostname
        hostAliases = local.in_cluster_host_aliases
        ingress = merge({
          enabled      = true
          prefix       = "/"
          ingressClass = "nginx"
          sslEnabled   = var.tls_enabled
          annotations  = local.service_ingress_annotations
        }, local.service_tls_block != null ? { tls = local.service_tls_block } : {})
        auth              = var.enable_auth ? local.auth_block : { enabled = false }
        extraEnv          = concat(local.db_env, local.storage_env, [{ name = "OSMO_SKIP_DATA_AUTH", value = "1" }])
        extraVolumes      = local.vault_extra_volumes
        extraVolumeMounts = local.vault_extra_mounts
      }
      worker = {
        scaling = {
          minReplicas = 1
          maxReplicas = 1
        }
        extraEnv          = concat(local.db_env, local.storage_env)
        extraVolumes      = local.vault_extra_volumes
        extraVolumeMounts = local.vault_extra_mounts
      }
      logger = {
        scaling = {
          minReplicas = 1
          maxReplicas = 1
        }
        hostAliases       = local.in_cluster_host_aliases
        extraEnv          = concat(local.db_env, local.storage_env)
        extraVolumes      = local.vault_extra_volumes
        extraVolumeMounts = local.vault_extra_mounts
      }
      agent = {
        scaling = {
          minReplicas = 1
          maxReplicas = 1
        }
        hostAliases       = local.in_cluster_host_aliases
        extraEnv          = concat(local.db_env, local.storage_env)
        extraVolumes      = local.vault_extra_volumes
        extraVolumeMounts = local.vault_extra_mounts
      }
      delayedJobMonitor = {
        replicas          = 1
        extraEnv          = local.db_env
        extraVolumes      = local.vault_extra_volumes
        extraVolumeMounts = local.vault_extra_mounts
      }
    }

    sidecars = local.service_sidecars
  }

  router_values = merge({
    global = {
      domain = var.ingress_hostname
    }
    service = {
      type = "ClusterIP"
    }
    services = {
      configFile = {
        enabled = true
      }
      postgres = {
        serviceName = local.postgres_host
        port        = local.postgres_port
        db          = local.postgres_db
        user        = local.postgres_user
      }
      redis = {
        serviceName = "redis-master.${var.namespace}.svc.cluster.local"
        port        = 6379
        tlsEnabled  = false
      }
      service = merge({
        hostname    = var.ingress_hostname
        hostAliases = local.in_cluster_host_aliases
        ingress = merge({
          enabled      = true
          ingressClass = "nginx"
          sslEnabled   = var.tls_enabled
          annotations  = var.enable_auth ? local.proxy_buffer_annotations : {}
        }, local.service_tls_block != null ? { tls = local.service_tls_block } : {})
        scaling = {
          minReplicas = 1
          maxReplicas = 1
        }
        }, var.tls_enabled && var.tls_mode == "cert-manager" ? {
        ingress = merge({
          enabled      = true
          ingressClass = "nginx"
          sslEnabled   = true
          annotations = merge(
            var.enable_auth ? local.proxy_buffer_annotations : {},
            {
              "nginx.ingress.kubernetes.io/ssl-redirect" = "true"
              "cert-manager.io/cluster-issuer"           = var.cluster_issuer_name
            }
          )
          tls = local.service_tls_block
        }, {})
      } : {})
    }
    sidecars = local.router_sidecars
  }, var.enable_auth ? {} : {})

  ui_values = {
    global = {
      domain = var.ingress_hostname
    }
    services = {
      redis = {
        serviceName = "redis-master.${var.namespace}.svc.cluster.local"
        port        = 6379
        tlsEnabled  = false
      }
      ui = merge({
        service = {
          type = "ClusterIP"
        }
        hostAliases = local.in_cluster_host_aliases
        ingress = merge({
          enabled      = true
          ingressClass = "nginx"
          sslEnabled   = var.tls_enabled
          annotations  = var.enable_auth ? local.proxy_buffer_annotations : {}
        }, local.service_tls_block != null ? { tls = local.service_tls_block } : {})
        replicas         = 1
        apiHostname      = var.tls_enabled ? var.ingress_hostname : "osmo-service.${var.namespace}.svc.cluster.local:80"
        hostname         = var.ingress_hostname
        nextjsSslEnabled = var.tls_enabled
        extraEnvs = [
          {
            name  = "NEXT_PUBLIC_OSMO_AUTH_HOSTNAME"
            value = local.auth_domain
          }
        ]
        }, var.tls_enabled && var.tls_mode == "cert-manager" ? {
        ingress = merge({
          enabled      = true
          ingressClass = "nginx"
          sslEnabled   = true
          annotations = merge(
            var.enable_auth ? local.proxy_buffer_annotations : {},
            {
              "nginx.ingress.kubernetes.io/ssl-redirect" = "true"
              "cert-manager.io/cluster-issuer"           = var.cluster_issuer_name
            }
          )
          tls = local.service_tls_block
        }, {})
      } : {})
    }
    sidecars = local.ui_sidecars
  }
}
