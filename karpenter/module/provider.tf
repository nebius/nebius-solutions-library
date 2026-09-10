terraform {
  required_providers {
    nebius = {
      source  = "terraform-provider.storage.eu-north1.nebius.cloud/nebius/nebius"
      version = ">= 0.5.139"
    }

    kubectl = {
      source  = "gavinbunney/kubectl"
      version = ">= 1.7.0"
    }
    
    helm = {
      source  = "hashicorp/helm"
      version = ">= 3.1.1"
    }
  }

  required_version = ">= 1.10.0"
}

provider "kubectl" {
  host                   = nebius_mk8s_v1_cluster.cluster.status.control_plane.endpoints.public_endpoint
  cluster_ca_certificate = nebius_mk8s_v1_cluster.cluster.status.control_plane.auth.cluster_ca_certificate
  token                  = ephemeral.nebius_iam_token.token.token
  load_config_file       = false
}

provider "helm" {
  kubernetes = {
    host                   = nebius_mk8s_v1_cluster.cluster.status.control_plane.endpoints.public_endpoint
    cluster_ca_certificate = nebius_mk8s_v1_cluster.cluster.status.control_plane.auth.cluster_ca_certificate
    token                  = ephemeral.nebius_iam_token.token.token
  }

  registries = [
    {
      url = "oci://cr.eu-north1.nebius.cloud/e00w67thrrz5nhprjm"

      username = "iam"
      password = ephemeral.nebius_iam_token.token.token
    }
  ]
}
