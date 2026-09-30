terraform {
  required_providers {
    kubernetes = {
      source = "hashicorp/kubernetes"
    }
    nebius = {
      source = "terraform-provider.storage.eu-north1.nebius.cloud/nebius/nebius"
    }
  }
}
