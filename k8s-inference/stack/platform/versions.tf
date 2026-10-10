terraform {
  required_version = ">= 1.12.0, < 2.0.0"
  backend "s3" {}
  required_providers {
    nebius = {
      source  = "nebius/nebius"
      version = ">= 0.6.23"
    }
    kubernetes    = { source = "hashicorp/kubernetes", version = "~> 3.0" }
    helm          = { source = "hashicorp/helm", version = "~> 3.0" }
    kubectl       = { source = "gavinbunney/kubectl", version = "~> 1.19" }
    kustomization = { source = "kbst/kustomization", version = "~> 0.9" }
    external      = { source = "hashicorp/external", version = "~> 2.3" }
    http          = { source = "hashicorp/http", version = "~> 3.4" }
    random        = { source = "hashicorp/random", version = "~> 3.6" }
  }
}
