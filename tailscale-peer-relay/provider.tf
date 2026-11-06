terraform {
  required_providers {
    nebius = {
      source = "terraform-provider.storage.eu-north1.nebius.cloud/nebius/nebius"
    }
  }
}

provider "nebius" {
  domain = "api.eu.nebius.cloud:443"
}
