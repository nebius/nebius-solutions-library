# Stage 3, models: tenants and models on ONE cluster (`-var target=<cluster id>`). Reads the cloud state
# (endpoints, projects, the tenant buckets and keys) and this cluster's platform state (URLs, master key).
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
