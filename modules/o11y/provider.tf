terraform {
  required_providers {
    nebius = {
      source = "terraform-provider.storage.eu-north1.nebius.cloud/nebius/nebius"
    }
    random = {
      source  = "hashicorp/random"
      version = "3.7.1"
    }
  }
}
