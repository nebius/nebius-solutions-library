terraform {
  required_version = ">= 1.8.0"

  required_providers {
    nebius = {
      source  = "terraform-provider.storage.eu-north1.nebius.cloud/nebius/nebius"
      version = ">= 0.5.174, < 0.6.0"
    }
  }
}
