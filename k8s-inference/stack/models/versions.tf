terraform {
  required_version = ">= 1.12.0, < 2.0.0"
  backend "s3" {}
  required_providers {
    nebius = {
      source  = "nebius/nebius"
      version = ">= 0.6.23"
    }
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 2.38" }
    helm       = { source = "hashicorp/helm", version = "~> 3.0" }
    random     = { source = "hashicorp/random", version = "~> 3.6" }
    external   = { source = "hashicorp/external", version = "~> 2.3" }
  }
}
