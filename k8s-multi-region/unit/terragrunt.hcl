terragrunt_version_constraint = "= 1.2.1"
terraform_version_constraint  = ">= 1.11.0, < 2.0.0"
terraform_binary              = "terraform"

terraform {
  # A pinned Git source with //k8s-training retains the sibling modules directory.
  source = values.source
}

remote_state {
  backend      = "s3"
  disable_init = true # Foundation owns the existing Nebius state bucket.
  generate = {
    path      = "backend.tf"
    if_exists = "overwrite_terragrunt"
  }
  config = values.backend
}

inputs = values.inputs
