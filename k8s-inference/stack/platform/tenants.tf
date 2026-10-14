# Tenants on this cluster (terraform.tfvars `tenants`): the namespace with its queues, identity and policies
# (charts/tenant), the storage Secrets from the buckets the cloud stage created, the NGC pull Secret and, on
# the control cluster, the LiteLLM API keys and the optional apply-time acceptance probe.
locals {
  all_regions = sort(keys(local.f.regions))
  # Tenants present on this cluster: every tenant on the control cluster, tenants of this region on a worker.
  tenants = { for tn, t in local.f.tenants : tn => t if local.role.control || contains(coalesce(t.regions, local.all_regions), local.region) }
  # Tenant buckets and keys are cloud-stage resources (stack/cloud/tenants.tf): this region's per tenant on a
  # worker, the hub region's everywhere (the primary bucket of fleet-placed runs and the control cluster's API).
  # try(): on `destroy` the cloud state may lack a region that is being removed; the values are irrelevant for
  # deleting the Secrets.
  hub_storage   = try(local.cloud.tenant_storage[local.hub_region], {})
  local_storage = try(local.cloud.tenant_storage[local.region], {})
  storage_here = { for tn, t in local.tenants : tn => local.role.worker ? local.local_storage[tn] : local.hub_storage[tn]
  if t.storage == "managed" && can(local.role.worker ? local.local_storage[tn] : local.hub_storage[tn]) }
  s3_env = { for tn, st in local.storage_here : tn => {
    AWS_ACCESS_KEY_ID = st.access_key, AWS_SECRET_ACCESS_KEY = st.secret_key, AWS_ENDPOINT_URL = st.endpoint, AWS_DEFAULT_REGION = st.region
  } }
}

# Namespace tenant-<name> with its run identity, queues, policy and API binding (charts/tenant). After the
# platform (the tenant's ClusterQueues come from charts/fleet, the API binding needs the ClusterRole of wave 3).
resource "helm_release" "tenant" {
  for_each = local.tenants
  name     = "tenant-${each.key}"
  chart    = "${local.repo}/charts/tenant"
  values = [yamlencode({
    name          = each.key
    cluster       = local.id
    fleet         = local.fleet_doc
    allowedImages = each.value.allowed_images
    assets        = each.value.assets # restricted assets granted to this tenant (charts/tenant assets.yaml)
    # execution options (charts/tenant execution.yaml, namespace annotations): identities, profiles, the fleet's API hosts
    identities        = each.value.identities
    executionProfiles = each.value.execution_profiles
    apiHosts          = [for cid, c in local.cloud.clusters : "api.${local.host_of[cid]}"]
    # Multi-node runs over InfiniBand need the tenant's ResourceClaimTemplates (charts/tenant multinode.yaml):
    # on every worker whose region has an InfiniBand pool, and on the control cluster whenever any region has one
    # (Kueue there resolves the template before MultiKueue dispatches). No tfvars input: the pools decide.
    infiniband = anytrue([for rn, r in local.f.regions : anytrue([for p in values(r.pools) : try(p.interconnect, null) == "infiniband"])
    if local.role.control || rn == local.region])
    clusters = { (local.id) = local.role.worker ? {
      region = local.region
      } : {
      region  = local.region
      manager = true
    } }
  })]
  wait       = true
  depends_on = [helm_release.wave3, kubectl_manifest.wave3, helm_release.fleet]
}

# tenant-storage: the bucket this cluster's API presigns, lists and bills from (the hub-region bucket on control).
resource "kubernetes_secret_v1" "tenant_storage" {
  for_each = { for tn, t in local.tenants : tn => t if contains(keys(local.storage_here), tn) }
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
  for_each = { for tn, t in local.tenants : tn => t if local.role.worker && t.storage == "managed" }
  metadata {
    name      = "s3"
    namespace = "tenant-${each.key}"
  }
  data       = local.s3_env[each.key]
  depends_on = [helm_release.tenant]
}

resource "kubernetes_secret_v1" "s3_fleet" {
  for_each = { for tn, t in local.tenants : tn => t if local.role.worker && t.storage == "managed" }
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

resource "kubernetes_secret_v1" "ngc" {
  for_each = local.role.worker && local.ngc_on ? local.tenants : {}
  metadata {
    name      = "ngc"
    namespace = "tenant-${each.key}"
  }
  type       = "kubernetes.io/dockerconfigjson"
  data       = { ".dockerconfigjson" = jsonencode({ auths = { "nvcr.io" = { username = "$oauthtoken", password = local.ngc_key, auth = base64encode("$oauthtoken:${local.ngc_key}") } } }) }
  depends_on = [helm_release.tenant]
}

# ---------------------------------------------------------------------------
# API keys (control cluster): one Job per key of terraform.tfvars writes the key into the fleet database with
# `python -m keys bootstrap` of the API image (services/api/keys.py: idempotent, the value Terraform generated,
# mirrored into LiteLLM for its OpenAI route) and so exists before any admin key could mint through the API.
# Values are sensitive outputs (`tenant_keys`). A `-replace` of a key's random_password rotates it (same alias, spend kept).
locals {
  keys = local.role.control ? merge([for tn, t in local.tenants : { for kn, k in t.keys : "${tn}-${kn}" => merge(k, { tenant = tn, key = kn }) }]...) : {}
  # the trust ConfigMap `api/trust` of the platform stage: the managed database's CA (and a private bundle, if any)
  key_job_mounts = concat([{ name = "trust", path = "/etc/ssl/msp", sub_path = null }],
  local.f.trust_bundle_pem == null ? [] : [{ name = "trust", path = "/etc/ssl/certs/ca-certificates.crt", sub_path = "ca.pem" }])
}

resource "random_password" "key" {
  for_each = local.keys
  length   = 40
  special  = false
}

# The value reaches the Job through a Secret: a Job's pod spec (readable by anyone who may read Jobs or pods in
# `api`) carries only the reference. The Jobs are kept (no TTL) so that an apply never re-runs them.
resource "kubernetes_secret_v1" "key" {
  for_each = local.keys
  metadata {
    name      = "key-${each.key}"
    namespace = "api"
  }
  data       = { KEY = "sk-${random_password.key[each.key].result}" }
  depends_on = [kubernetes_namespace_v1.ns]
}

resource "kubernetes_job_v1" "key" {
  for_each = local.keys
  metadata {
    name      = "key-${each.key}"
    namespace = "api"
  }
  spec {
    backoff_limit = 6
    template {
      metadata {}
      spec {
        restart_policy = "OnFailure"
        container {
          name    = "key"
          image   = local.image.api
          command = ["python", "-m", "keys", "bootstrap"]
          env {
            name = "KEY"
            value_from {
              secret_key_ref {
                name = "key-${each.key}"
                key  = "KEY"
              }
            }
          }
          env {
            name = "KEY_SPEC"
            value = jsonencode({
              alias  = each.key, tenant = each.value.tenant, role = each.value.admin ? "admin" : "user",
              budget = each.value.budget_usd, models = each.value.models, routes = each.value.routes
            })
          }
          env {
            name = "DATABASE_URL"
            value_from {
              secret_key_ref {
                name = "database"
                key  = "platform_url"
              }
            }
          }
          env {
            name  = "LITELLM_URL"
            value = "http://litellm.litellm.svc:4000"
          }
          env {
            name = "LITELLM_MASTER_KEY"
            value_from {
              secret_key_ref {
                name = "litellm-master"
                key  = "masterkey"
              }
            }
          }
          dynamic "volume_mount" {
            for_each = local.key_job_mounts
            content {
              name       = volume_mount.value.name
              mount_path = volume_mount.value.path
              sub_path   = volume_mount.value.sub_path
              read_only  = true
            }
          }
        }
        volume {
          name = "trust"
          config_map { name = "trust" }
        }
      }
    }
  }
  wait_for_completion = true
  timeouts { create = "20m" }
  depends_on = [helm_release.wave3, kubernetes_secret_v1.database, kubernetes_config_map_v1.trust, kubernetes_secret_v1.litellm_master, kubernetes_secret_v1.key]
}

# ---------------------------------------------------------------------------
# Acceptance (control cluster): tools/verify.sh runs the built-in run class `acceptance.model` with the first
# tenant key and, with `acceptance.example_endpoint`, defines the example model `llm-example`
# (stack/platform/llm-example.json: a stock vLLM container on the fleet's first GPU class) through the API with
# the first admin key and calls it. The operator runs it after the apply (`./stack.sh output platform control
# -json acceptance` gives it the inputs); `acceptance.probe = true` also runs the same script as a Job at apply
# time, and the apply waits for it (up to 40 minutes on a cold region) and fails with it.
locals {
  first_key   = length(local.keys) > 0 ? sort(keys(local.keys))[0] : null
  admin_keys  = sort([for k, v in local.keys : k if v.admin])
  first_admin = length(local.admin_keys) > 0 ? local.admin_keys[0] : null
  probe_on    = local.f.acceptance.probe && local.role.control && local.first_key != null
  example_on  = local.role.control && local.f.acceptance.example_endpoint && local.first_admin != null && length(local.gpu_classes) > 0
  example_spec = merge(jsondecode(file("${path.module}/llm-example.json")), {
    gpu     = { count = 1, classes = [local.gpu_classes[0]] }
    regions = sort(distinct([for id, c in local.region_clusters : c.region if length([for pn, p in c.pools : pn if p.gpu_class == local.gpu_classes[0]]) > 0]))
  })
  acceptance = local.role.control ? {
    api_url   = try("https://${local.hostnames.api}", null)
    model     = local.f.acceptance.model
    key       = local.first_key == null ? null : "sk-${random_password.key[local.first_key].result}"
    admin_key = local.first_admin == null ? null : "sk-${random_password.key[local.first_admin].result}"
    example   = local.example_on
    spec      = local.example_on ? local.example_spec : null
  } : null
  verify_script = file("${local.repo}/tools/verify.sh")
}

resource "kubernetes_config_map_v1" "probe" {
  count = local.probe_on ? 1 : 0
  metadata {
    name      = "acceptance-probe"
    namespace = "api"
  }
  data       = { "verify.sh" = local.verify_script }
  depends_on = [kubernetes_namespace_v1.ns]
}

resource "kubernetes_secret_v1" "probe" {
  count = local.probe_on ? 1 : 0
  metadata {
    name      = "acceptance-probe"
    namespace = "api"
  }
  data       = { KEY = local.acceptance.key, ADMIN_KEY = coalesce(local.acceptance.admin_key, "") }
  depends_on = [kubernetes_namespace_v1.ns]
}

resource "kubernetes_job_v1" "probe" {
  count = local.probe_on ? 1 : 0
  metadata {
    name      = "acceptance-probe"
    namespace = "api"
    labels    = { "serverless2.nebius/script" = substr(sha256(local.verify_script), 0, 16) } # a changed script re-runs the probe
  }
  spec {
    backoff_limit = 0
    template {
      metadata {}
      spec {
        restart_policy = "Never"
        container {
          name    = "probe"
          image   = "${local.registry.docker}/curlimages/curl:8.10.1"
          command = ["sh", "/scripts/verify.sh"]
          env {
            name  = "API"
            value = local.acceptance.api_url
          }
          env {
            name = "KEY"
            value_from {
              secret_key_ref {
                name = "acceptance-probe"
                key  = "KEY"
              }
            }
          }
          env {
            name = "ADMIN_KEY"
            value_from {
              secret_key_ref {
                name = "acceptance-probe"
                key  = "ADMIN_KEY"
              }
            }
          }
          env {
            name  = "MODEL"
            value = local.acceptance.model
          }
          env {
            name  = "EXAMPLE"
            value = local.example_on ? "1" : "0"
          }
          env {
            name  = "SPEC"
            value = local.example_on ? jsonencode(local.example_spec) : ""
          }
          volume_mount {
            name       = "scripts"
            mount_path = "/scripts"
            read_only  = true
          }
        }
        volume {
          name = "scripts"
          config_map { name = "acceptance-probe" }
        }
      }
    }
  }
  wait_for_completion = true
  timeouts { create = "40m" }
  depends_on = [kubernetes_job_v1.key, kubernetes_secret_v1.tenant_storage, helm_release.tenant, kubectl_manifest.control_extras, kubernetes_config_map_v1.probe, kubernetes_secret_v1.probe]
}
