terraform {
  required_providers {
    nebius = {
      source  = "terraform-provider.storage.eu-north1.nebius.cloud/nebius/nebius"
      version = ">= 0.5.55"
    }
    helm = {
      source  = "hashicorp/helm"
      version = ">= 2.12.0"
    }
    kubernetes = {
      source = "hashicorp/kubernetes"
    }
    units = {
      source  = "dstaroff/units"
      version = ">= 1.1.1"
    }
  }
}
