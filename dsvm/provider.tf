terraform {
  required_providers {
    nebius = {
      source = "nebius/nebius"
    }
  }
}

provider "nebius" {
  domain = "api.eu.nebius.cloud:443"
}
