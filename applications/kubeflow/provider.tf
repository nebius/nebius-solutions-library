terraform {
  required_providers {
    nebius = {
      source = "terraform-provider.storage.eu-north1.nebius.cloud/nebius/nebius"
    }
    kubernetes = {
      source = "hashicorp/kubernetes"
    }
  }
}

provider "time" {}

resource "time_static" "start" {}

provider "kubernetes" {
  host                   = module.k8s-training.kube_cluster.endpoints.public_endpoint
  cluster_ca_certificate = module.k8s-training.kube_cluster_ca_certificate
  token                  = var.iam_token
}
