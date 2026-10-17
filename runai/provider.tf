terraform {
  # >= 1.11 is required because modules/gpu-operator uses the nebius provider's
  # write-only `sensitive` block, which Terraform only supports from 1.11
  # (older versions fail every plan with a provider error).
  required_version = ">= 1.11"
  required_providers {
    nebius = {
      source  = "nebius/nebius"
      version = ">= 0.6.23, < 1.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 3.3"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.3"
    }
    tls = {
      source  = "hashicorp/tls"
      version = ">= 4.0, < 5.0"
    }
  }
}

provider "helm" {
  kubernetes = {
    host                   = nebius_mk8s_v1_cluster.k8s-cluster.status.control_plane.endpoints.public_endpoint
    cluster_ca_certificate = nebius_mk8s_v1_cluster.k8s-cluster.status.control_plane.auth.cluster_ca_certificate
    token                  = var.iam_token
  }
}

provider "kubernetes" {
  host                   = nebius_mk8s_v1_cluster.k8s-cluster.status.control_plane.endpoints.public_endpoint
  cluster_ca_certificate = nebius_mk8s_v1_cluster.k8s-cluster.status.control_plane.auth.cluster_ca_certificate
  token                  = var.iam_token
}
