terraform {
  required_version = ">= 1.11.4"

  required_providers {
    nebius = {
      source = "nebius/nebius"
    }
    random = {
      source  = "hashicorp/random"
      version = "3.7.1"
    }
  }
}
