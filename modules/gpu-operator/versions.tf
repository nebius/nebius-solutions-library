terraform {
  # The module uses the nebius provider's write-only `sensitive` block, which
  # requires Terraform >= 1.11. Callers (k8s-training, skypilot, ...) have no
  # terraform version floor of their own, so it is enforced here to fail early
  # with a clear message instead of a cryptic provider error.
  required_version = ">= 1.11"

  required_providers {
    nebius = {
      source = "nebius/nebius"
    }
  }
}
