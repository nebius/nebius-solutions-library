terraform {
  required_providers {
    external = {
      source = "hashicorp/external"
    }
    nebius = {
      source  = "nebius/nebius"
      version = ">= 0.6.31"
    }
  }
}
