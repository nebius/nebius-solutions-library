terraform {
  required_providers {
    nebius = {
      source = "nebius/nebius"
    }
    random = {
      source  = "hashicorp/random"
      version = "3.7.1"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.0"
    }
  }
}
