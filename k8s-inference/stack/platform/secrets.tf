# Namespaces, Secrets and cross-cluster identities. Values come from the cloud state (generated once) and
# from the environment (secrets.*_env in terraform.tfvars); nothing is read from files in the repository and
# nothing here calls a cloud API: a plan of this stage issues no credential.
locals {
  # knative-serving: the KnativeServing CR (wave 2) lives there; created here before the operator reconciles it.
  # registry: the image cache's namespace, only with the cache (images.cache.enabled).
  namespaces = concat(["ops", "monitoring", "api", "models", "spegel", "kueue-system", "knative-serving"], local.images_cache ? ["registry"] : [], local.role.control ? ["litellm", "ui-app"] : [])
}

# `registry` is also rendered by charts/fleet (release fleet in kueue-system); created here with Helm's
# ownership metadata so the release adopts it instead of refusing the install.
resource "kubernetes_namespace_v1" "ns" {
  for_each = toset(local.namespaces)
  metadata {
    name = each.key
    labels = merge(
      each.key == "models" ? { "serverless2.nebius/kueue" = "managed" } : {},
      each.key == "registry" ? { "app.kubernetes.io/managed-by" = "Helm" } : {}
    )
    annotations = each.key == "registry" ? { "meta.helm.sh/release-name" = "fleet", "meta.helm.sh/release-namespace" = "kueue-system" } : {}
  }
  lifecycle {
    ignore_changes = [metadata[0].labels, metadata[0].annotations]
  }
}

# Secret inputs (config.tf: sensitive variables stack.sh fills from the environment variables named in
# terraform.tfvars `secrets`; an empty name switches the feature off; a named variable that is unset stops
# stack.sh before any plan). The booleans below are what `count` reads: a sensitive value itself cannot drive it.
locals {
  # an empty name in the tfvars switches the secret off (null never reaches here: the schema's defaults apply);
  # coalesce() is not used on these: it rejects an all-empty call
  ngc_off        = local.f.secrets.ngc_api_key_env == null || local.f.secrets.ngc_api_key_env == ""
  hf_off         = local.f.secrets.hf_token_env == null || local.f.secrets.hf_token_env == ""
  registry_off   = local.f.secrets.registry_credentials_env == null || local.f.secrets.registry_credentials_env == ""
  ngc_key        = local.ngc_off || var.ngc_api_key == null ? "" : var.ngc_api_key
  hf_token       = local.hf_off || var.hf_token == null ? "" : var.hf_token
  extra_registry = local.registry_off || var.registry_credentials == null || var.registry_credentials == "" ? {} : jsondecode(var.registry_credentials)
  ngc_on         = nonsensitive(local.ngc_key != "")
  hf_on          = nonsensitive(local.hf_token != "")
  # the image cache's static key (variable registry_key): with the cache on, stack.sh refuses to run while the named
  # variable is unset; a direct terraform call without it is stopped by the precondition on the Secret below
  registry_key = var.registry_key == null ? "" : var.registry_key
}

check "secret_inputs_present" {
  assert {
    condition     = (local.ngc_off || local.ngc_key != "") && (local.hf_off || local.hf_token != "")
    error_message = "A secret named in terraform.tfvars `secrets` is empty: export it (stack.sh maps it to TF_VAR_ngc_api_key / TF_VAR_hf_token) or set its name to \"\" to run without it. Nothing is applied with a missing secret."
  }
}

# Ops service account credentials of this cluster's project (the ops image's nebius profile).
resource "kubernetes_secret_v1" "nebius_sa" {
  metadata {
    name      = "nebius-sa"
    namespace = "ops"
  }
  data       = { "${local.region}.json" = local.secrets.ops_credentials[local.cluster.project_id] }
  depends_on = [kubernetes_namespace_v1.ns]
}

# The dispatcher and its price feed (control cluster): the ops identity of every GPU region's project, one
# `<region>.json` per region as in the ops image (the dispatcher raises an InfiniBand pool's floor in the region's
# project while a multi-node run waits for it, docs/SCHEDULING.md "InfiniBand pools grow on demand"; the price
# calculator accepts any of them).
resource "kubernetes_secret_v1" "price_feed_sa" {
  count = local.role.manager ? 1 : 0
  metadata {
    name      = "price-feed-nebius-sa"
    namespace = "kueue-system"
  }
  data       = { for cid, c in local.region_clusters : "${c.region}.json" => local.secrets.ops_credentials[c.project_id] }
  depends_on = [kubernetes_namespace_v1.ns]
}

resource "kubernetes_secret_v1" "grafana_admin" {
  metadata {
    name      = "grafana-admin"
    namespace = "monitoring"
  }
  data       = { admin-user = "admin", admin-password = local.secrets.grafana_admin_password }
  depends_on = [kubernetes_namespace_v1.ns]
}

resource "kubernetes_secret_v1" "cost_export_s3" {
  metadata {
    name      = "cost-export-s3"
    namespace = "monitoring"
  }
  data       = { ACCESS_KEY_ID = local.secrets.backups_s3.access_key_id, ACCESS_SECRET_KEY = local.secrets.backups_s3.secret }
  depends_on = [kubernetes_namespace_v1.ns]
}

# The LiteLLM master key: the API (the control cluster only since api 0.11.18) reads LiteLLM's key records with it
# until every key is imported into the fleet database (services/api/auth.py). The GPU regions had a copy for their
# regional API; the moved block keeps the control cluster's state address and removes the copies.
moved {
  from = kubernetes_secret_v1.litellm_master_api
  to   = kubernetes_secret_v1.litellm_master_api[0]
}

resource "kubernetes_secret_v1" "litellm_master_api" {
  count = local.role.control ? 1 : 0
  metadata {
    name      = "litellm-master"
    namespace = "api"
  }
  data       = { masterkey = local.secrets.litellm_master_key }
  depends_on = [kubernetes_namespace_v1.ns]
}

resource "kubernetes_secret_v1" "litellm_master" {
  count = local.role.control ? 1 : 0
  metadata {
    name      = "litellm-master"
    namespace = "litellm"
  }
  data       = { masterkey = local.secrets.litellm_master_key }
  depends_on = [kubernetes_namespace_v1.ns]
}

# NGC: pull Secret and API key in the models namespace (NIM images, seed-weights), when a key is given.
resource "kubernetes_secret_v1" "ngc_pull" {
  count = local.ngc_on && local.role.worker ? 1 : 0
  metadata {
    name      = "ngc"
    namespace = "models"
  }
  type       = "kubernetes.io/dockerconfigjson"
  data       = { ".dockerconfigjson" = jsonencode({ auths = { "nvcr.io" = { username = "$oauthtoken", password = local.ngc_key, auth = base64encode("$oauthtoken:${local.ngc_key}") } } }) }
  depends_on = [kubernetes_namespace_v1.ns]
}

resource "kubernetes_secret_v1" "ngc_api_key" {
  count = local.ngc_on && local.role.worker ? 1 : 0
  metadata {
    name      = "ngc-api-key"
    namespace = "models"
  }
  data       = { NGC_API_KEY = local.ngc_key }
  depends_on = [kubernetes_namespace_v1.ns]
}

resource "kubernetes_secret_v1" "hf_token" {
  count = local.hf_on && local.role.worker ? 1 : 0
  metadata {
    name      = "hf-token"
    namespace = "models"
  }
  data       = { HF_TOKEN = local.hf_token }
  depends_on = [kubernetes_namespace_v1.ns]
}

# Image cache credentials (only with the cache, images.cache.enabled): the static Container Registry key of the hub
# ops SA (variable registry_key, from the environment: issued once per fleet by the operator with
# scripts/registry-static-key.sh, the same value on every cluster) plus NGC and extra upstreams.
locals {
  registry_host = split("/", local.cloud.hub.registry)[0]
  zot_credentials = merge(
    { (local.registry_host) = { username = "iam", password = local.registry_key } },
    local.ngc_on == false ? {} : { "nvcr.io" = { username = "$oauthtoken", password = local.ngc_key } },
    local.extra_registry,
  )
}

resource "kubernetes_secret_v1" "zot_sync" {
  count = local.images_cache ? 1 : 0
  metadata {
    name      = "zot-sync-credentials"
    namespace = "registry"
  }
  data       = { "credentials.json" = jsonencode(local.zot_credentials) }
  depends_on = [kubernetes_namespace_v1.ns]
  lifecycle {
    precondition {
      condition     = local.registry_key != ""
      error_message = "images.cache is enabled but the registry static key is empty: export the variable named by secrets.registry_key_env (default REGISTRY_STATIC_KEY; issue the key once per fleet with stack/scripts/registry-static-key.sh). A plan never issues it."
    }
  }
}

# The model catalog the API serves (/etc/catalog): one file per entry, from stack/config locals.
resource "kubernetes_config_map_v1" "catalog" {
  metadata {
    name      = "catalog"
    namespace = "api"
  }
  data       = { for mid, e in local.catalog : "${mid}.yaml" => yamlencode(e) }
  depends_on = [kubernetes_namespace_v1.ns]
}

# ---------------------------------------------------------------------------
# Worker identities the control cluster uses: Kueue MultiKueue (kueue-system/multikueue) and the fleet API
# (api/api-agent), each with a long-lived ServiceAccount token (no rotation CronJob; rotate with
# `terraform apply -replace`). Published as sensitive outputs; the control stage reads them. The objects are the
# chart's components api-agent, fleet-access and tenant-clusterrole (manifests.tf), applied before the waves.
resource "kubectl_manifest" "identities" {
  for_each          = { for k, r in local.platform_objects : k => jsonencode(r.object) if local.role.worker && contains(["api-agent", "fleet-access", "tenant-clusterrole"], r.component) }
  yaml_body         = each.value
  server_side_apply = true
  force_conflicts   = true
  wait              = false
  depends_on        = [kubernetes_namespace_v1.ns]
}

# Control cluster: the same tenant ClusterRole (the fleet API creates manager Jobs in tenant namespaces and
# reads `tenant-storage`); workers get it with the identities above.
resource "kubectl_manifest" "tenant_clusterrole" {
  for_each          = { for k, r in local.platform_objects : k => jsonencode(r.object) if local.role.control && r.component == "tenant-clusterrole" }
  yaml_body         = each.value
  server_side_apply = true
  force_conflicts   = true
  wait              = false
}

resource "kubernetes_secret_v1" "sa_token" {
  for_each = local.role.worker ? { multikueue = "kueue-system", api-agent = "api" } : {}
  metadata {
    name        = "${each.key}-token"
    namespace   = each.value
    annotations = { "kubernetes.io/service-account.name" = each.key }
  }
  type                           = "kubernetes.io/service-account-token"
  wait_for_service_account_token = true
  depends_on                     = [kubectl_manifest.identities]
}

locals {
  worker_kubeconfig = { for k, s in kubernetes_secret_v1.sa_token : k => yamlencode({
    apiVersion = "v1", kind = "Config", "current-context" = local.id
    clusters   = [{ name = local.id, cluster = { server = local.cluster.endpoint, "certificate-authority-data" = base64encode(local.cluster.cluster_ca_certificate) } }]
    users      = [{ name = k, user = { token = s.data["token"] } }]
    contexts   = [{ name = local.id, context = { cluster = local.id, user = k } }]
  }) }
}

# Control cluster: the workers' kubeconfigs from their platform states.
data "terraform_remote_state" "worker" {
  for_each = local.role.manager ? local.region_clusters : {}
  backend  = "s3"
  config = {
    bucket                      = local.state_bucket
    key                         = "platform/${each.key}.tfstate"
    region                      = local.state_region
    endpoints                   = { s3 = local.state_endpoint }
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
    skip_s3_checksum            = true
  }
}

resource "kubernetes_secret_v1" "multikueue_remote" {
  for_each = local.role.manager ? local.region_clusters : {}
  metadata {
    name      = "multikueue-${each.key}"
    namespace = "kueue-system"
  }
  data       = { kubeconfig = data.terraform_remote_state.worker[each.key].outputs.multikueue_kubeconfig }
  depends_on = [kubernetes_namespace_v1.ns]
}

resource "kubernetes_secret_v1" "region_kubeconfigs" {
  count = local.role.manager ? 1 : 0
  metadata {
    name      = "region-kubeconfigs"
    namespace = "api"
  }
  data       = { for cid, c in local.region_clusters : c.region => data.terraform_remote_state.worker[cid].outputs.api_agent_kubeconfig }
  depends_on = [kubernetes_namespace_v1.ns]
}
