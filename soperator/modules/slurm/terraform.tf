terraform {
  required_providers {
    helm = {
      source  = "hashicorp/helm"
      version = "<3.0.0"
    }
    kubernetes = {
      source = "hashicorp/kubernetes"
    }
    local = {
      source  = "hashicorp/local"
      version = "2.5.3"
    }
  }
}

module "labels" {
  source = "../labels"
}
