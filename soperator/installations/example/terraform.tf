terraform {
  required_version = ">=1.8.0"

  required_providers {
    nebius = {
      source  = "terraform-provider.storage.eu-north1.nebius.cloud/nebius/nebius"
      version = ">=0.4"
    }

    flux = {
      source  = "fluxcd/flux"
      version = ">= 1.5"
    }

    units = {
      source  = "dstaroff/units"
      version = ">=1.1.1"
    }

    kubernetes = {
      source = "hashicorp/kubernetes"
    }

    helm = {
      source = "hashicorp/helm"
      version = "<2.0.0"
    }
  }
}

provider "nebius" {
  domain = "api.eu.nebius.cloud:443"
}

provider "units" {}

provider "kubernetes" {
  host                   = module.k8s.control_plane.public_endpoint
  cluster_ca_certificate = module.k8s.control_plane.cluster_ca_certificate
  token                  = var.iam_token
}

provider "flux" {
  kubernetes = {
    host                   = module.k8s.control_plane.public_endpoint
    cluster_ca_certificate = module.k8s.control_plane.cluster_ca_certificate
    token                  = var.iam_token
  }
  git = {
    url = "ssh://git@github.com/${var.github_org}/${var.github_repository}.git"
    ssh = {
      username = "git"
    }
  }
}

provider "helm" {
  kubernetes {
    host                   = module.k8s.control_plane.public_endpoint
    cluster_ca_certificate = module.k8s.control_plane.cluster_ca_certificate
    token                  = var.iam_token
  }
}

module "resources" {
  source = "../../modules/available_resources"
}
