locals {
  backend_operator_login_method_effective = coalesce(
    var.backend_operator_login_method,
    (var.enable_auth ? "password" : "token")
  )

  backend_operator_keycloak_ca_secret_name = "backend-operator-keycloak-ca"

  backend_operator_self_signed_cert_enabled = (
    var.deploy_backend_operator
    && var.enable_auth
    && var.tls_enabled
    && var.tls_mode == "self-signed"
  )

  backend_operator_self_signed_extra_envs = [
    {
      name  = "REQUESTS_CA_BUNDLE"
      value = "/opt/osmo/certs/keycloak-ca.crt"
    },
    {
      name  = "SSL_CERT_FILE"
      value = "/opt/osmo/certs/keycloak-ca.crt"
    },
  ]

  backend_operator_self_signed_secret_volume = {
    name = "keycloak-ca"
    secret = {
      secretName = local.backend_operator_keycloak_ca_secret_name
    }
  }

  backend_operator_self_signed_secret_mount = {
    name      = "keycloak-ca"
    mountPath = "/opt/osmo/certs/keycloak-ca.crt"
    subPath   = "ca.crt"
    readOnly  = true
  }

  backend_operator_progress_volume = {
    name     = "progress-files"
    emptyDir = {}
  }

  backend_operator_progress_mount = {
    name      = "progress-files"
    mountPath = "/var/run/osmo"
  }

  backend_operator_service_url_value = coalesce(
    var.backend_operator_service_url,
    "http://osmo-agent.${var.namespace}.svc.cluster.local:80"
  )

  backend_operator_values = {
    global = merge(
      var.osmo_image_tag != "latest" ? {
        osmoImageTag = var.osmo_image_tag
      } : {},
      {
        serviceUrl       = local.backend_operator_service_url_value
        agentNamespace   = var.backend_operator_namespace
        backendNamespace = var.workflows_namespace
        backendName      = var.backend_name
        nodeSelector = {
          "kubernetes.io/os" = "linux"
        }
        logs = {
          logLevel    = "DEBUG"
          k8sLogLevel = "WARNING"
        }
      },
      local.backend_operator_login_method_effective == "password" ? {
        accountUsername          = var.backend_operator_username
        accountPasswordSecret    = var.backend_operator_password_secret_name
        accountPasswordSecretKey = var.backend_operator_password_secret_key
        loginMethod              = "password"
        } : {
        accountTokenSecret    = "osmo-operator-token"
        accountTokenSecretKey = "token"
        loginMethod           = "token"
      }
    )

    services = {
      backendListener = merge({
        resources = {
          requests = {
            cpu    = "100m"
            memory = "256Mi"
          }
          limits = {
            memory = "1Gi"
          }
        }
        extraEnvs = local.backend_operator_self_signed_cert_enabled ? local.backend_operator_self_signed_extra_envs : []
        volumes = concat(
          [local.backend_operator_progress_volume],
          local.backend_operator_self_signed_cert_enabled ? [local.backend_operator_self_signed_secret_volume] : []
        )
        volumeMounts = concat(
          [local.backend_operator_progress_mount],
          local.backend_operator_self_signed_cert_enabled ? [local.backend_operator_self_signed_secret_mount] : []
        )
      })
      backendWorker = merge({
        resources = {
          requests = {
            cpu    = "100m"
            memory = "256Mi"
          }
          limits = {
            memory = "1Gi"
          }
        }
        extraEnvs = local.backend_operator_self_signed_cert_enabled ? local.backend_operator_self_signed_extra_envs : []
        volumes = concat(
          [local.backend_operator_progress_volume],
          local.backend_operator_self_signed_cert_enabled ? [local.backend_operator_self_signed_secret_volume] : []
        )
        volumeMounts = concat(
          [local.backend_operator_progress_mount],
          local.backend_operator_self_signed_cert_enabled ? [local.backend_operator_self_signed_secret_mount] : []
        )
      })
    }

    backendTestRunner = {
      enabled = false
    }
  }
}

resource "terraform_data" "cleanup_stale_backend_operator_release" {
  count = var.deploy_backend_operator ? 1 : 0

  input = {
    namespace    = var.backend_operator_namespace
    release_name = var.backend_operator_release_name
  }

  triggers_replace = {
    namespace    = var.backend_operator_namespace
    release_name = var.backend_operator_release_name
  }

  depends_on = [
    kubernetes_namespace_v1.backend_operator,
  ]

  provisioner "local-exec" {
    command = "/bin/bash ${path.module}/../scripts/cleanup-stale-helm-release.sh"

    environment = {
      KUBECONFIG      = pathexpand(var.kubeconfig_path)
      KUBECTL_CONTEXT = var.kubeconfig_context != null ? var.kubeconfig_context : ""
      NAMESPACE       = var.backend_operator_namespace
      RELEASE_NAME    = var.backend_operator_release_name
    }
  }
}

resource "helm_release" "backend_operator" {
  count = var.deploy_backend_operator ? 1 : 0

  name            = var.backend_operator_release_name
  namespace       = var.backend_operator_namespace
  repository      = "https://helm.ngc.nvidia.com/nvidia/osmo"
  chart           = "backend-operator"
  version         = var.backend_operator_chart_version
  values          = [yamlencode(local.backend_operator_values)]
  atomic          = true
  cleanup_on_fail = true
  timeout         = 1200

  depends_on = [
    terraform_data.validate,
    terraform_data.cleanup_stale_backend_operator_release,
    kubernetes_namespace_v1.backend_operator,
    kubernetes_namespace_v1.workflows,
    kubernetes_secret_v1.backend_operator_password,
    kubernetes_secret_v1.backend_operator_keycloak_ca,
    terraform_data.backend_operator_token_secret,
    helm_release.osmo_service,
    terraform_data.post_install,
  ]
}
