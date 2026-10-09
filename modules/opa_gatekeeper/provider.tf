terraform {
  required_providers {
    http = {
      source  = "hashicorp/http"
      version = "3.5.0"
    }
    kubectl = {
      source  = "gavinbunney/kubectl"
      version = ">=1.19.0"
    }
  }
}
