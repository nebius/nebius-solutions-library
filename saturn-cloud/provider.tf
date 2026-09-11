locals {
  # The Nebius API endpoint is keyed by region GROUP (api.eu / api.us / ...), not the
  # full region: api.us-central1.nebius.cloud has no DNS, and IAM (a global control
  # plane) is only reachable via the group endpoint. Map each region to its group.
  nebius_region_groups = {
    eu-north1   = "eu"
    eu-north2   = "eu"
    eu-west1    = "eu"
    me-west1    = "eu" # Middle East regions are served by the eu API group
    uk-south1   = "eu"
    us-central1 = "us"
  }
  nebius_region_group = lookup(local.nebius_region_groups, var.region, "eu")
  nebius_api_domain   = "api.${local.nebius_region_group}.nebius.cloud:443"
}

provider "nebius" {
  domain = local.nebius_api_domain
}

provider "helm" {
  kubernetes = {
    host                   = nebius_mk8s_v1_cluster.saturn_cluster.status.control_plane.endpoints.public_endpoint
    cluster_ca_certificate = nebius_mk8s_v1_cluster.saturn_cluster.status.control_plane.auth.cluster_ca_certificate
    token                  = var.iam_token
  }
}

provider "kubernetes" {
  host                   = nebius_mk8s_v1_cluster.saturn_cluster.status.control_plane.endpoints.public_endpoint
  cluster_ca_certificate = nebius_mk8s_v1_cluster.saturn_cluster.status.control_plane.auth.cluster_ca_certificate
  token                  = var.iam_token
}
