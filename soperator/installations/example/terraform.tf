terraform {
  required_version = ">=1.12.0"

  required_providers {
    nebius = {
      source  = "terraform-provider.storage.eu-north1.nebius.cloud/nebius/nebius"
      version = "= 0.5.230"
    }

    flux = {
      source  = "fluxcd/flux"
      version = ">= 1.5"
    }

    units = {
      source  = "dstaroff/units"
      version = ">=1.1.1"
    }

    string-functions = {
      source  = "random-things/string-functions"
      version = "0.5.0"
    }

    kubernetes = {
      source = "hashicorp/kubernetes"
    }

    helm = {
      source  = "hashicorp/helm"
      version = "<3.0.0"
    }
  }
}

provider "nebius" {
  domain            = "api.eu.nebius.cloud:443"
  timeout           = "10m"
  per_retry_timeout = "1m"
  retries           = 10
}

provider "units" {}

provider "string-functions" {}

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
