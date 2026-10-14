terraform {
  required_version = ">= 1.12.0"

  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = ">= 3.0.1"
    }
    helm = {
      source  = "hashicorp/helm"
      version = ">= 2.17.0, < 4.0.0"
    }
  }
}
