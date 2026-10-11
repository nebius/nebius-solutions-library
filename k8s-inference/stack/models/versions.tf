terraform {
  required_version = ">= 1.12.0, < 2.0.0"
  backend "s3" {}
  required_providers {
    nebius = {
      source  = "nebius/nebius"
      version = ">= 0.6.23"
    }
    # the same major as the platform stage: the Secret and Job resources used here (kubernetes_secret_v1,
    # kubernetes_job_v1) are unchanged in 3.x (the 3.0 upgrade guide removes only kubernetes_pod_security_policy)
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 3.0" }
    helm       = { source = "hashicorp/helm", version = "~> 3.0" }
    random     = { source = "hashicorp/random", version = "~> 3.6" }
    external   = { source = "hashicorp/external", version = "~> 2.3" }
  }
}
