terraform {
  required_version = ">= 1.12.0, < 2.0.0"
  # State in the fleet's Object Storage bucket; stack.sh passes bucket, key and endpoint at init.
  backend "s3" {}
  required_providers {
    nebius = {
      source  = "nebius/nebius"
      version = ">= 0.6.23"
    }
    random = { source = "hashicorp/random", version = "~> 3.6" }
    tls    = { source = "hashicorp/tls", version = "~> 4.0" }
  }
}

# Authentication: NEBIUS_IAM_TOKEN in the environment (`nebius iam get-access-token`; stack.sh exports it),
# the same way as the library's other solutions. No CLI profile is named anywhere in the configuration.
provider "nebius" {}
