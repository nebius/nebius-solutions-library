# The platform-internal LiteLLM key (control cluster). The API registers every OpenAI endpoint it defines as a
# LiteLLM model group whose deployment is the endpoint's gateway hostname, and the edge on those hostnames
# requires a LiteLLM key: this key is that caller. A LiteLLM virtual key of the platform (alias
# `platform-internal`, no budget, no expiry, every model), never handed to a tenant; LiteLLM applies the real
# caller's key (budget, rate limit, spend) before it routes to the group (docs/API.md "Model groups").
# Value generated here: Secret api/litellm-internal (the API reads LITELLM_INTERNAL_KEY, stage.tf) and a copy
# litellm/litellm-internal for the Job that registers it once LiteLLM is up (wave 3). Idempotent.
resource "random_password" "litellm_internal" {
  count   = local.role.control ? 1 : 0
  length  = 40
  special = false
}

resource "kubernetes_secret_v1" "litellm_internal" {
  for_each = local.role.control ? toset(["api", "litellm"]) : toset([])
  metadata {
    name      = "litellm-internal"
    namespace = each.key
  }
  data       = { key = "sk-${random_password.litellm_internal[0].result}" }
  depends_on = [kubernetes_namespace_v1.ns]
}

resource "kubernetes_job_v1" "litellm_internal_key" {
  count = local.role.control ? 1 : 0
  metadata {
    name      = "key-platform-internal"
    namespace = "litellm"
  }
  spec {
    backoff_limit = 6
    template {
      metadata {}
      spec {
        restart_policy = "OnFailure"
        container {
          name    = "key"
          image   = "${local.images_host}/docker/curlimages/curl:8.10.1"
          command = ["sh", "-c"]
          args = [<<-EOT
            set -eu
            body="{\"key\":\"$KEY\",\"key_alias\":\"platform-internal\",\"models\":[],\"metadata\":{\"tenant\":\"platform\",\"role\":\"internal\"}}"
            for i in $(seq 1 60); do
              code=$(curl -s -o /tmp/r -w '%%{http_code}' -X POST "$LITELLM/key/generate" -H "Authorization: Bearer $MASTER" -H 'content-type: application/json' -d "$body" || echo 000)
              case "$code" in
                200) echo "key platform-internal created"; exit 0 ;;
                400|409) if grep -qiE 'exist|duplicate' /tmp/r; then echo "key platform-internal exists"; exit 0; fi; cat /tmp/r; exit 1 ;;
                *) echo "attempt $i: HTTP $code"; sleep 10 ;;
              esac
            done
            exit 1
          EOT
          ]
          env {
            name  = "LITELLM"
            value = local.litellm_url_in_cluster
          }
          env {
            name = "MASTER"
            value_from {
              secret_key_ref {
                name = "litellm-master"
                key  = "masterkey"
              }
            }
          }
          env {
            name = "KEY"
            value_from {
              secret_key_ref {
                name = "litellm-internal"
                key  = "key"
              }
            }
          }
        }
      }
    }
  }
  wait_for_completion = true
  timeouts { create = "20m" }
  depends_on = [helm_release.wave3, kubernetes_secret_v1.litellm_internal]
}
