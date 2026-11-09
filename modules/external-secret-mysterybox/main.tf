locals {
  external_secret_name = (
    var.external_secret_name != null && trimspace(var.external_secret_name) != ""
    ? trimspace(var.external_secret_name)
    : var.target_secret_name
  )

  secret_store_manifest = {
    apiVersion = "external-secrets.io/v1"
    kind       = var.secret_store_kind
    metadata = merge(
      {
        name = var.secret_store_name
      },
      var.secret_store_kind == "SecretStore" ? {
        namespace = var.namespace
      } : {}
    )
    spec = {
      provider = {
        nebiusmysterybox = merge(
          {
            apiDomain = var.api_domain
            auth = {
              serviceAccountCredsSecretRef = {
                name = var.service_account_credentials_secret_name != null ? trimspace(var.service_account_credentials_secret_name) : ""
                key  = var.service_account_credentials_secret_key != null ? trimspace(var.service_account_credentials_secret_key) : ""
              }
            }
          },
          var.ca_provider_secret_name != null && trimspace(var.ca_provider_secret_name) != "" ? {
            caProvider = {
              certSecretRef = {
                name = trimspace(var.ca_provider_secret_name)
                key  = trimspace(var.ca_provider_secret_key)
              }
            }
          } : {}
        )
      }
    }
  }

  external_secret_manifest = {
    apiVersion = "external-secrets.io/v1"
    kind       = "ExternalSecret"
    metadata = {
      name      = local.external_secret_name
      namespace = var.namespace
    }
    spec = merge(
      {
        refreshInterval = var.refresh_interval
        secretStoreRef = {
          kind = var.secret_store_kind
          name = var.secret_store_name
        }
        target = merge(
          {
            name           = var.target_secret_name
            creationPolicy = var.creation_policy
          },
          var.deletion_policy != null && trimspace(var.deletion_policy) != "" ? {
            deletionPolicy = var.deletion_policy
          } : {}
        )
      },
      var.mysterybox_secret_version != null && trimspace(var.mysterybox_secret_version) != "" ? {
        dataFrom = [{
          extract = {
            key     = var.mysterybox_secret_id
            version = var.mysterybox_secret_version
          }
        }]
        } : {
        dataFrom = [{
          extract = {
            key = var.mysterybox_secret_id
          }
        }]
      }
    )
  }
}

resource "kubernetes_namespace_v1" "this" {
  count = var.create_namespace ? 1 : 0

  metadata {
    name = var.namespace
  }
}

resource "kubectl_manifest" "secret_store" {
  count = var.create_secret_store ? 1 : 0

  yaml_body = yamlencode(local.secret_store_manifest)

  lifecycle {
    precondition {
      condition = (
        var.service_account_credentials_secret_name != null &&
        trimspace(var.service_account_credentials_secret_name) != "" &&
        var.service_account_credentials_secret_key != null &&
        trimspace(var.service_account_credentials_secret_key) != ""
      )
      error_message = "service_account_credentials_secret_name and service_account_credentials_secret_key must be set when create_secret_store is true."
    }
  }

  depends_on = [
    kubernetes_namespace_v1.this,
  ]
}

resource "kubectl_manifest" "external_secret" {
  yaml_body = yamlencode(local.external_secret_manifest)

  depends_on = [
    kubernetes_namespace_v1.this,
    kubectl_manifest.secret_store,
  ]
}
