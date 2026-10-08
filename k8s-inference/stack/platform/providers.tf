# Stage 2, platform: everything inside ONE cluster (`-var target=<cluster id>`, one state per cluster). The
# cluster's endpoint and CA come from the cloud state; authentication is the Nebius CLI's exec plugin, exactly
# what `nebius mk8s cluster get-credentials` writes into a kubeconfig.

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
  cloud     = data.terraform_remote_state.cloud.outputs
  cluster   = local.cloud.clusters[var.target]
  role      = local.roles[var.target]
  secrets   = local.cloud.secrets
  exec_args = ["mk8s", "v1", "cluster", "get-token", "--profile", local.f.nebius_profile, "--format", "json"]
  kubeconfig = yamlencode({
    apiVersion = "v1", kind = "Config", "current-context" = var.target
    clusters   = [{ name = var.target, cluster = { server = local.cluster.endpoint, "certificate-authority-data" = base64encode(local.cluster.cluster_ca_certificate) } }]
    users      = [{ name = var.target, user = { exec = { apiVersion = "client.authentication.k8s.io/v1beta1", command = "nebius", args = local.exec_args, interactiveMode = "Never", provideClusterInfo = false } } }]
    contexts   = [{ name = var.target, context = { cluster = var.target, user = var.target } }]
  })
}

provider "nebius" {
  profile = {
    name            = local.f.nebius_profile
    no_browser_open = true
  }
}

provider "kubernetes" {
  host                   = local.cluster.endpoint
  cluster_ca_certificate = local.cluster.cluster_ca_certificate
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "nebius"
    args        = local.exec_args
  }
}

provider "helm" {
  kubernetes = {
    host                   = local.cluster.endpoint
    cluster_ca_certificate = local.cluster.cluster_ca_certificate
    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "nebius"
      args        = local.exec_args
    }
  }
}

provider "kubectl" {
  host                   = local.cluster.endpoint
  cluster_ca_certificate = local.cluster.cluster_ca_certificate
  load_config_file       = false
  apply_retry_count      = 5
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "nebius"
    args        = local.exec_args
  }
}

provider "kustomization" {
  kubeconfig_raw = local.kubeconfig
}
