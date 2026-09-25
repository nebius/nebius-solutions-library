terraform {
  # Requires >= 1.10.0 for ephemeral resources (MysteryBox integration)
  # Requires >= 1.11.0 for write-only sensitive fields (PostgreSQL password)
  required_version = ">= 1.11.0"

  required_providers {
    nebius = {
      source = "terraform-provider.storage.eu-north1.nebius.cloud/nebius/nebius"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.0"
    }
    units = {
      source  = "dstaroff/units"
      version = ">= 1.1.1"
    }
  }
}

provider "nebius" {
  domain = "api.eu.nebius.cloud:443"
}

provider "random" {}
