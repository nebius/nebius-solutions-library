# Stage 2, platform: everything inside ONE cluster (`-var target=<cluster id>`, one state per cluster). The
# cluster's endpoint and CA come from the cloud state; authentication is the IAM token stack.sh takes from the
# Nebius CLI (`nebius iam get-access-token`) and passes as the sensitive variable `iam_token`. It configures
# the Kubernetes, Helm, kubectl and kustomization providers only: provider configuration is never written to
# the state, and no resource takes the token as an input (the local-exec provisioners read NEBIUS_IAM_TOKEN
# from the environment, stack/scripts/kube.sh).

data "terraform_remote_state" "cloud" {
  backend = "s3"
  config = {
    bucket                      = local.state_bucket
    key                         = "cloud/fleet.tfstate"
    region                      = local.state_region
    endpoints                   = { s3 = local.state_endpoint }
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
    skip_s3_checksum            = true
  }
}

locals {
  cloud   = data.terraform_remote_state.cloud.outputs
  cluster = local.cloud.clusters[var.target]
  role    = local.roles[var.target]
  secrets = local.cloud.secrets
  kubeconfig = yamlencode({
    apiVersion = "v1", kind = "Config", "current-context" = var.target
    clusters   = [{ name = var.target, cluster = { server = local.cluster.endpoint, "certificate-authority-data" = base64encode(local.cluster.cluster_ca_certificate) } }]
    users      = [{ name = var.target, user = { token = var.iam_token } }]
    contexts   = [{ name = var.target, context = { cluster = var.target, user = var.target } }]
  })
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

provider "kubectl" {
  host                   = local.cluster.endpoint
  cluster_ca_certificate = local.cluster.cluster_ca_certificate
  load_config_file       = false
  apply_retry_count      = 5
  token                  = var.iam_token
}

provider "kustomization" {
  kubeconfig_raw = local.kubeconfig
}
