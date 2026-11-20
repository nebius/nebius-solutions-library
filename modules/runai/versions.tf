terraform {
  required_version = ">= 1.11.0"
  required_providers {
    nebius = {
      source = "nebius/nebius"
    }
    kubernetes = {
      source = "hashicorp/kubernetes"
    }
    kubectl = {
      source = "gavinbunney/kubectl"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.0"
    }
  }
}
