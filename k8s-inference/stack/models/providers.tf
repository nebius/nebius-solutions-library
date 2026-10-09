# Stage 3, models: tenants and models on ONE cluster (`-var target=<cluster id>`). Reads the cloud state
# (endpoints, projects) and this cluster's platform state (URLs, master key), plus the hub worker's models
# state for the tenants' primary (hub-region) storage on the other clusters.
locals {
  backend_common = {
    bucket                      = local.state_bucket
    region                      = local.state_region
    endpoints                   = { s3 = local.state_endpoint }
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
    skip_s3_checksum            = true
  }
}

data "terraform_remote_state" "cloud" {
  backend = "s3"
  config  = merge(local.backend_common, { key = "cloud/fleet.tfstate" })
}

data "terraform_remote_state" "platform" {
  backend = "s3"
  config  = merge(local.backend_common, { key = "platform/${var.target}.tfstate" })
}

# The hub worker's models state (the tenants' hub-region buckets): read on every cluster but the hub itself.
data "terraform_remote_state" "hub_models" {
  count   = var.target == local.hub_id ? 0 : 1
  backend = "s3"
  config  = merge(local.backend_common, { key = "models/${local.hub_id}.tfstate" })
}

# The control cluster's probe needs the workers' endpoints to exist: their models states are applied first.
data "terraform_remote_state" "worker_models" {
  for_each = local.roles[var.target].control && local.dedicated ? local.region_clusters : {}
  backend  = "s3"
  config   = merge(local.backend_common, { key = "models/${each.key}.tfstate" })
}

locals {
  cloud    = data.terraform_remote_state.cloud.outputs
  platform = data.terraform_remote_state.platform.outputs
  cluster  = local.cloud.clusters[var.target]
  role     = local.roles[var.target]
}

provider "nebius" {} # NEBIUS_IAM_TOKEN from the environment (stack.sh exports it)

provider "kubernetes" {
  host                   = local.cluster.endpoint
  cluster_ca_certificate = local.cluster.cluster_ca_certificate
  token                  = var.iam_token
}

provider "helm" {
  kubernetes = {
    host                   = local.cluster.endpoint
    cluster_ca_certificate = local.cluster.cluster_ca_certificate
    token                  = var.iam_token
  }
}
