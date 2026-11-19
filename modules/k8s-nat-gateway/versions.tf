terraform {
  required_version = ">= 1.7.0"

  required_providers {
    kubectl = {
      source  = "gavinbunney/kubectl"
      version = ">= 1.19.0"
    }
    nebius = {
      source  = "nebius/nebius"
      version = ">= 0.6.70"
    }
  }
}
