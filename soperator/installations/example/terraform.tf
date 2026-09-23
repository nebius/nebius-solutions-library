terraform {
  required_version = ">=1.12.0"

  required_providers {
    nebius = {
      source  = "nebius/nebius"
      version = ">= 0.5.232"
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
  profile           = {}
}

locals {
  kubernetes_exec = {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "nebius"
    args        = ["mk8s", "v1", "cluster", "get-token", "--format", "json"]
  }
}

provider "units" {}

provider "string-functions" {}

provider "kubernetes" {
  host                   = module.k8s.control_plane.public_endpoint
  cluster_ca_certificate = module.k8s.control_plane.cluster_ca_certificate
  exec {
    api_version = local.kubernetes_exec.api_version
    command     = local.kubernetes_exec.command
    args        = local.kubernetes_exec.args
  }
}

provider "flux" {
  kubernetes = {
    host                   = module.k8s.control_plane.public_endpoint
    cluster_ca_certificate = module.k8s.control_plane.cluster_ca_certificate
    exec                   = local.kubernetes_exec
  }
}

provider "helm" {
  kubernetes {
    host                   = module.k8s.control_plane.public_endpoint
    cluster_ca_certificate = module.k8s.control_plane.cluster_ca_certificate
    exec {
      api_version = local.kubernetes_exec.api_version
      command     = local.kubernetes_exec.command
      args        = local.kubernetes_exec.args
    }
  }
}

module "resources" {
  source = "../../modules/available_resources"
}
