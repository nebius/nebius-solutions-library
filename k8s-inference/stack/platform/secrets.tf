# Namespaces, Secrets and cross-cluster identities. Values come from the cloud state (generated once) and
# from the environment (secrets.*_env in terraform.tfvars); nothing is read from files in the repository.
locals {
  # knative-serving: the KnativeServing CR (wave 2) lives there; Argo CD used to create it (CreateNamespace=true).
  namespaces = concat(["ops", "monitoring", "api", "models", "registry", "spegel", "kueue-system", "knative-serving"], local.role.control ? ["data", "litellm", "ui-app"] : [])
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

# Environment-provided secrets (names from terraform.tfvars `secrets`; empty when unset).
data "external" "env" {
  program = ["bash", "${path.module}/../scripts/env.sh", local.f.secrets.ngc_api_key_env, local.f.secrets.hf_token_env, local.f.secrets.registry_credentials_env]
}

locals {
  ngc_key        = data.external.env.result.ngc
  hf_token       = data.external.env.result.hf
  extra_registry = data.external.env.result.registries == "" ? {} : jsondecode(data.external.env.result.registries)
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

resource "kubernetes_secret_v1" "price_feed_sa" {
  count = local.role.manager ? 1 : 0
  metadata {
    name      = "price-feed-nebius-sa"
    namespace = "kueue-system"
  }
  data       = { "${local.region}.json" = local.secrets.ops_credentials[local.cluster.project_id] }
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

# The LiteLLM master key: the API of every cluster validates keys with it.
resource "kubernetes_secret_v1" "litellm_master_api" {
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

resource "kubernetes_secret_v1" "litellm_db" {
  for_each = local.role.control ? toset(["data", "litellm"]) : toset([])
  metadata {
    name      = "litellm-db"
    namespace = each.key
  }
  data       = { username = "litellm", password = local.secrets.litellm_db_password }
  depends_on = [kubernetes_namespace_v1.ns]
}

resource "kubernetes_secret_v1" "backup_s3" {
  count = local.role.control ? 1 : 0
  metadata {
    name      = "backup-s3"
    namespace = "data"
  }
  data       = { ACCESS_KEY_ID = local.secrets.backups_s3.access_key_id, ACCESS_SECRET_KEY = local.secrets.backups_s3.secret }
  depends_on = [kubernetes_namespace_v1.ns]
}

# NGC: pull Secret and API key in the models namespace (NIM images, seed-weights), when a key is given.
resource "kubernetes_secret_v1" "ngc_pull" {
  count = local.ngc_key != "" && local.role.worker ? 1 : 0
  metadata {
    name      = "ngc"
    namespace = "models"
  }
  type       = "kubernetes.io/dockerconfigjson"
  data       = { ".dockerconfigjson" = jsonencode({ auths = { "nvcr.io" = { username = "$oauthtoken", password = local.ngc_key, auth = base64encode("$oauthtoken:${local.ngc_key}") } } }) }
  depends_on = [kubernetes_namespace_v1.ns]
}

resource "kubernetes_secret_v1" "ngc_api_key" {
  count = local.ngc_key != "" && local.role.worker ? 1 : 0
  metadata {
    name      = "ngc-api-key"
    namespace = "models"
  }
  data       = { NGC_API_KEY = local.ngc_key }
  depends_on = [kubernetes_namespace_v1.ns]
}

resource "kubernetes_secret_v1" "hf_token" {
  count = local.hf_token != "" && local.role.worker ? 1 : 0
  metadata {
    name      = "hf-token"
    namespace = "models"
  }
  data       = { HF_TOKEN = local.hf_token }
  depends_on = [kubernetes_namespace_v1.ns]
}

# Image cache credentials: a static Container Registry key of the hub ops SA (issued once per operator by the
# Nebius CLI, cached under stack/.secrets; see scripts/registry-static-key.sh) plus NGC and extra upstreams.
data "external" "registry_key" {
  program = ["bash", "${path.module}/../scripts/registry-static-key.sh"]
  query = {
    profile = local.f.nebius_profile
    project = local.hub_project
    sa_id   = local.cloud.clusters[local.hub_id].ops_service_account_id
    name    = "${local.name}-image-cache"
    cache   = "${local.repo}/stack/.secrets/${local.name}-registry-key.json"
  }
}

locals {
  registry_host = split("/", local.cloud.hub.registry)[0]
  zot_credentials = merge(
    { (local.registry_host) = { username = "iam", password = data.external.registry_key.result.key } },
    local.ngc_key == "" ? {} : { "nvcr.io" = { username = "$oauthtoken", password = local.ngc_key } },
    local.extra_registry,
  )
}

resource "kubernetes_secret_v1" "zot_sync" {
  metadata {
    name      = "zot-sync-credentials"
    namespace = "registry"
  }
  data       = { "credentials.json" = jsonencode(local.zot_credentials) }
  depends_on = [kubernetes_namespace_v1.ns]
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
# `terraform apply -replace`). Published as sensitive outputs; the control stage reads them.
data "kustomization_overlay" "identities" {
  count     = local.role.worker ? 1 : 0
  resources = ["../../clusters/common/manifests/fleet-access/multikueue.yaml", "../../clusters/common/manifests/api-agent"]
  kustomize_options {
    load_restrictor = "none"
  }
}

resource "kubectl_manifest" "identities" {
  for_each          = local.role.worker ? data.kustomization_overlay.identities[0].manifests : {}
  yaml_body         = each.value
  server_side_apply = true
  force_conflicts   = true
  wait              = false
  depends_on        = [kubernetes_namespace_v1.ns]
}

# Control cluster: the same tenant ClusterRole (the fleet API creates manager Jobs in tenant namespaces and
# reads `tenant-storage`); workers get it from the api-agent kustomization above.
data "kustomization_overlay" "tenant_clusterrole" {
  count     = local.role.control ? 1 : 0
  resources = ["../../clusters/common/manifests/api-agent/tenant-clusterrole"]
  kustomize_options {
    load_restrictor = "none"
  }
}

resource "kubectl_manifest" "tenant_clusterrole" {
  for_each          = local.role.control ? data.kustomization_overlay.tenant_clusterrole[0].manifests : {}
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
