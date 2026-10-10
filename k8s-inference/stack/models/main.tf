locals {
  repo   = abspath("${path.module}/../..")
  id     = var.target
  region = local.cluster.region
  name   = local.f.name

  all_regions = sort(keys(local.f.regions))
  # Tenants present on this cluster: every tenant on the control cluster, tenants of this region on a worker.
  tenants = { for tn, t in local.f.tenants : tn => t if local.role.control || contains(coalesce(t.regions, local.all_regions), local.region) }
  # Storage of the hub region per tenant: created here on the hub, read from the hub's models state elsewhere.
  # Lookups below use try(): on `destroy` the hub's models state may already be gone (empty map), and the
  # values are irrelevant for deleting the Secrets.
  hub_storage = local.id == local.hub_id ? { for tn, m in module.tenant_region : tn => m.storage } : try(data.terraform_remote_state.hub_models[0].outputs.tenant_storage, {})

  fleet_doc = local.chart_fleet # charts/tenant reads regions, pools and prices from it (stack/config/locals.tf)
}

# Cloud side of a tenant in this region (workers only): identity, bucket, access key.
module "tenant_region" {
  for_each       = local.role.worker ? local.tenants : {}
  source         = "../modules/tenant-region"
  name           = "${local.name}-${each.key}-${local.region}"
  tenant         = each.key
  region         = local.region
  project_id     = local.cluster.project_id
  lifecycle_days = each.value.lifecycle_days
  protect_data   = local.f.protect_data
  labels         = merge(local.f.labels, { "serverless2.nebius/tenant" = each.key })
}

# Namespace tenant-<name> with its run identity, queues, policy and API binding (charts/tenant).
resource "helm_release" "tenant" {
  for_each = local.tenants
  name     = "tenant-${each.key}"
  chart    = "${local.repo}/charts/tenant"
  values = [yamlencode({
    name          = each.key
    cluster       = local.id
    fleet         = local.fleet_doc
    allowedImages = each.value.allowed_images
    # Multi-node runs over InfiniBand need the tenant's ResourceClaimTemplates (charts/tenant multinode.yaml):
    # on every worker whose region has an InfiniBand pool, and on the control cluster whenever any region has one
    # (Kueue there resolves the template before MultiKueue dispatches). No tfvars input: the pools decide.
    infiniband = anytrue([for rn, r in local.f.regions : anytrue([for p in values(r.pools) : try(p.interconnect, null) == "infiniband"])
    if local.role.control || rn == local.region])
    clusters = { (local.id) = local.role.worker ? {
      region   = local.region
      bucket   = module.tenant_region[each.key].bucket
      endpoint = module.tenant_region[each.key].bucket_host
      } : {
      region  = local.region
      manager = true
    } }
  })]
  wait = true
}

locals {
  storage_here = { for tn, t in local.tenants : tn => local.role.worker ? module.tenant_region[tn].storage : try(local.hub_storage[tn], null) if local.role.worker || can(local.hub_storage[tn]) }
  s3_env = { for tn, st in local.storage_here : tn => {
    AWS_ACCESS_KEY_ID = st.access_key, AWS_SECRET_ACCESS_KEY = st.secret_key, AWS_ENDPOINT_URL = st.endpoint, AWS_DEFAULT_REGION = st.region
  } }
}

# tenant-storage: the bucket this cluster's API presigns, lists and bills from (the hub-region bucket on control).
resource "kubernetes_secret_v1" "tenant_storage" {
  for_each = local.tenants
  metadata {
    name      = "tenant-storage"
    namespace = "tenant-${each.key}"
  }
  data       = local.storage_here[each.key]
  depends_on = [helm_release.tenant]
}

# s3: AWS_* for the runner containers (this region's bucket); s3-fleet: the hub region's bucket, read and
# written by fleet-placed runs whose region is chosen after submission.
resource "kubernetes_secret_v1" "s3" {
  for_each = local.role.worker ? local.tenants : {}
  metadata {
    name      = "s3"
    namespace = "tenant-${each.key}"
  }
  data       = local.s3_env[each.key]
  depends_on = [helm_release.tenant]
}

resource "kubernetes_secret_v1" "s3_fleet" {
  for_each = local.role.worker ? local.tenants : {}
  metadata {
    name      = "s3-fleet"
    namespace = "tenant-${each.key}"
  }
  data = {
    AWS_ACCESS_KEY_ID     = try(local.hub_storage[each.key].access_key, "")
    AWS_SECRET_ACCESS_KEY = try(local.hub_storage[each.key].secret_key, "")
    AWS_ENDPOINT_URL      = try(local.hub_storage[each.key].endpoint, "")
    AWS_DEFAULT_REGION    = try(local.hub_storage[each.key].region, "")
  }
  depends_on = [helm_release.tenant]
}

data "external" "env" {
  program = ["bash", "${path.module}/../scripts/env.sh", local.f.secrets.ngc_api_key_env, local.f.secrets.hf_token_env, local.f.secrets.registry_credentials_env]
}

resource "kubernetes_secret_v1" "ngc" {
  for_each = local.role.worker && data.external.env.result.ngc != "" ? local.tenants : {}
  metadata {
    name      = "ngc"
    namespace = "tenant-${each.key}"
  }
  type       = "kubernetes.io/dockerconfigjson"
  data       = { ".dockerconfigjson" = jsonencode({ auths = { "nvcr.io" = { username = "$oauthtoken", password = data.external.env.result.ngc, auth = base64encode("$oauthtoken:${data.external.env.result.ngc}") } } }) }
  depends_on = [helm_release.tenant]
}

# ---------------------------------------------------------------------------
# LiteLLM API keys (control cluster): one Job per key POSTs /key/generate with a Terraform-generated key value
# (idempotent: an existing key is accepted). Values are sensitive outputs.
locals {
  keys = local.role.control ? merge([for tn, t in local.tenants : { for kn, k in t.keys : "${tn}-${kn}" => merge(k, { tenant = tn, key = kn }) }]...) : {}
}

resource "random_password" "key" {
  for_each = local.keys
  length   = 40
  special  = false
}

resource "kubernetes_job_v1" "litellm_key" {
  for_each = local.keys
  metadata {
    name      = "key-${each.key}"
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
          image   = "${local.f.images.host}/docker/curlimages/curl:8.10.1"
          command = ["sh", "-c"]
          args = [<<-EOT
            set -eu
            body='${jsonencode({
            key      = "sk-${random_password.key[each.key].result}", key_alias = each.key, max_budget = each.value.budget_usd, models = each.value.models,
            metadata = merge({ tenant = each.value.tenant, allowed_passthrough_routes = each.value.routes }, each.value.admin ? { role = "admin" } : {})
      })}'
            for i in $(seq 1 60); do
              code=$(curl -s -o /tmp/r -w '%%{http_code}' -X POST "$LITELLM/key/generate" -H "Authorization: Bearer $MASTER" -H 'content-type: application/json' -d "$body" || echo 000)
              case "$code" in
                200) echo "key ${each.key} created"; exit 0 ;;
                400|409) if grep -qiE 'exist|duplicate' /tmp/r; then echo "key ${each.key} exists"; exit 0; fi; cat /tmp/r; exit 1 ;;
                *) echo "attempt $i: HTTP $code"; sleep 10 ;;
              esac
            done
            exit 1
          EOT
    ]
    env {
      name  = "LITELLM"
      value = "http://litellm.litellm.svc:4000"
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
  }
}
}
}
wait_for_completion = true
timeouts { create = "20m" }
}

# ---------------------------------------------------------------------------
# Acceptance probe (control cluster, last): a run of `acceptance.model` with the first tenant key and, with
# `acceptance.example_endpoint`, the example model `llm-example` (stack/models/llm-example.json: a stock
# vLLM container, on the fleet's first GPU class) defined through the API with the first admin key, then
# called. A failure fails the apply. The example stays as the first model of the console.
locals {
  first_key   = length(local.keys) > 0 ? sort(keys(local.keys))[0] : null
  admin_keys  = sort([for k, v in local.keys : k if v.admin])
  first_admin = length(local.admin_keys) > 0 ? local.admin_keys[0] : null
  probe_on    = local.f.acceptance.probe && local.role.control && local.first_key != null
  example_on  = local.probe_on && local.f.acceptance.example_endpoint && local.first_admin != null && length(local.gpu_classes) > 0
  example_spec = merge(jsondecode(file("${path.module}/llm-example.json")), {
    gpu     = { count = 1, classes = [local.gpu_classes[0]] }
    regions = sort(distinct([for id, c in local.region_clusters : c.region if length([for pn, p in c.pools : pn if p.gpu_class == local.gpu_classes[0]]) > 0]))
  })
}

resource "kubernetes_job_v1" "probe" {
  count = local.probe_on ? 1 : 0
  metadata {
    name      = "acceptance-probe"
    namespace = "api"
  }
  spec {
    backoff_limit = 0
    template {
      metadata {}
      spec {
        restart_policy = "Never"
        container {
          name    = "probe"
          image   = "${local.f.images.host}/docker/curlimages/curl:8.10.1"
          command = ["sh", "-c"]
          args = [<<-EOT
            set -eu
            H="Authorization: Bearer $KEY"
            echo "== models"; curl -sf -H "$H" "$API/v1/models" > /tmp/models.json; grep -q '"${local.f.acceptance.model}"' /tmp/models.json
            echo "== run ${local.f.acceptance.model}"
            id=$(curl -sf -X POST -H "$H" -H 'content-type: application/json' -d '{"mode":"run","name":"acceptance-probe","input":{}}' "$API/v1/models/${local.f.acceptance.model}:invoke" | sed -n 's/.*"id":"\([^"]*\)".*/\1/p')
            echo "operation $id"
            for i in $(seq 1 120); do
              st=$(curl -sf -H "$H" "$API/v1/operations/$id" | sed -n 's/.*"status":"\([A-Z]*\)".*/\1/p')
              echo "$(date -u +%T) $st"
              case "$st" in SUCCEEDED) break ;; FAILED|CANCELLED) exit 1 ;; esac
              sleep 10
            done
            [ "$st" = SUCCEEDED ]
            %{if local.example_on~}
            echo "== example model ${local.example_spec.id}: defined through the API (admin key), then called"
            code=$(curl -s -o /tmp/m -w '%%{http_code}' -X POST -H "Authorization: Bearer $ADMIN_KEY" -H 'content-type: application/json' -d "$SPEC" "$API/v1/models" || echo 000)
            echo "POST /v1/models HTTP $code"; head -c 600 /tmp/m; echo
            case "$code" in 201) ;; 409) echo "exists already (kept)" ;; *) exit 1 ;; esac
            for i in $(seq 1 40); do   # a first cold start in a fresh region pulls the 10 GB image once (measured 11 min, 2026-10-09)
              code=$(curl -s -o /tmp/e -w '%%{http_code}' --max-time 660 -X POST -H "$H" -H 'content-type: application/json' \
                -d '{"mode":"sync","input":{"messages":[{"role":"user","content":"Say hello in three words."}],"max_tokens":16}}' \
                "$API/v1/models/${local.example_spec.id}:invoke" || echo 000)
              echo "$(date -u +%T) HTTP $code"; [ "$code" = 200 ] && { head -c 400 /tmp/e; echo; exit 0; }
              sleep 30
            done
            exit 1
            %{endif~}
          EOT
          ]
          env {
            name  = "API"
            value = local.platform.api_url
          }
          env {
            name  = "KEY"
            value = "sk-${random_password.key[local.first_key].result}"
          }
          env {
            name  = "ADMIN_KEY"
            value = local.first_admin != null ? "sk-${random_password.key[local.first_admin].result}" : ""
          }
          env {
            name  = "SPEC"
            value = jsonencode(local.example_spec)
          }
        }
      }
    }
  }
  wait_for_completion = true
  timeouts { create = "40m" }
  depends_on = [kubernetes_job_v1.litellm_key, kubernetes_secret_v1.tenant_storage, data.terraform_remote_state.worker_models]
}
