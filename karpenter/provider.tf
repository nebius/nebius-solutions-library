terraform {
  required_providers {
    nebius = {
      source  = "terraform-provider.storage.eu-north1.nebius.cloud/nebius/nebius"
      version = ">= 0.5.139"
    }
  }
}

provider "nebius" {
}

