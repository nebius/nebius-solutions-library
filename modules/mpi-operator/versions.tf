terraform {
  required_providers {
    http = {
      source = "hashicorp/http"
    }
    kubectl = {
      source  = "gavinbunney/kubectl"
      version = ">= 1.19.0"
    }
  }
}
