terraform {
  required_version = ">= 1.8.0"

  required_providers {
    nebius = {
      source  = "nebius/nebius"
      version = ">= 0.6.0"
    }
  }
}

provider "nebius" {
  domain = "api.eu.nebius.cloud:443"
}
