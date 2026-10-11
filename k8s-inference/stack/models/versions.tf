terraform {
  required_version = ">= 1.12.0, < 2.0.0"
  backend "s3" {}
  required_providers {
    # stays on 2.x until its live Secret and Job resources get a planned state upgrade (the platform stage is on 3.x)
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 2.38" }
    helm       = { source = "hashicorp/helm", version = "~> 3.0" }
    random     = { source = "hashicorp/random", version = "~> 3.6" }
    external   = { source = "hashicorp/external", version = "~> 2.3" }
  }
}
