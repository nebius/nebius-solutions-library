terraform {
  required_providers {
    nebius = {
      source = "terraform-provider.storage.eu-north1.nebius.cloud/nebius/nebius"
    }
    kubernetes = {
      source = "hashicorp/kubernetes"
    }
    units = {
      source  = "dstaroff/units"
      version = ">=1.1.1"
    }

    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.0"
    }
    kubectl = {
      source  = "gavinbunney/kubectl"
      version = ">=1.19.0"
    }
  }
}

provider "nebius" {
  domain = "api.eu.nebius.cloud:443"
}

provider "helm" {
  kubernetes = {
    host                   = nebius_mk8s_v1_cluster.k8s-cluster[local.primary-cluster-key].status.control_plane.endpoints.public_endpoint
    cluster_ca_certificate = nebius_mk8s_v1_cluster.k8s-cluster[local.primary-cluster-key].status.control_plane.auth.cluster_ca_certificate
    token                  = var.iam_token
  }
}

provider "kubernetes" {
  host                   = nebius_mk8s_v1_cluster.k8s-cluster[local.primary-cluster-key].status.control_plane.endpoints.public_endpoint
  cluster_ca_certificate = nebius_mk8s_v1_cluster.k8s-cluster[local.primary-cluster-key].status.control_plane.auth.cluster_ca_certificate
  token                  = var.iam_token
}

provider "kubectl" {
  apply_retry_count      = 5
  host                   = nebius_mk8s_v1_cluster.k8s-cluster[local.primary-cluster-key].status.control_plane.endpoints.public_endpoint
  cluster_ca_certificate = nebius_mk8s_v1_cluster.k8s-cluster[local.primary-cluster-key].status.control_plane.auth.cluster_ca_certificate
  token                  = var.iam_token
  load_config_file       = false
}
