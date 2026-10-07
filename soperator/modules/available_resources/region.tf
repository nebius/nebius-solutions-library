locals {
  regions = {
    eu-north1   = "eu-north1"
    eu-north2   = "eu-north2"
    eu-west1    = "eu-west1"
    eu-west2    = "eu-west2"
    me-west1    = "me-west1"
    uk-south1   = "uk-south1"
    us-central1 = "us-central1"
    us-north1   = "us-north1"
    beta        = "beta"
    omega       = "omega"
  }

  # Keep all names available to storage matrices, but expose only the selected
  # environment's regions to installation validation.
  supported_regions = var.cloud_environment == "testing" ? [
    local.regions.beta,
    local.regions.omega,
  ] : [for name, region in local.regions : region if !contains(["beta", "omega"], name)]
}
