terraform {
  required_version = ">= 1.11.4"

  required_providers {
    local = {
      source  = "hashicorp/local"
      version = "~> 2.9"
    }
    nebius = {
      source = "nebius/nebius"
    }
    random = {
      source  = "hashicorp/random"
      version = "3.7.1"
    }
  }
}
