locals {
  ssh_public_key = var.ssh_public_key.key != null ? var.ssh_public_key.key : (
  fileexists(var.ssh_public_key.path) ? file(var.ssh_public_key.path) : null)

  regions_default = {
    eu-west1 = {
      platform = "cpu-d3"
      preset   = "16vcpu-64gb"
    }
    eu-north1 = {
      platform = "cpu-d3"
      preset   = "16vcpu-64gb"
    }

    eu-west2 = {
      platform = "cpu-d3"
      preset   = "16vcpu-64gb"
    }

    eu-north2 = {
      platform = "cpu-d3"
      preset   = "16vcpu-64gb"
    }

    us-central1 = {
      platform = "cpu-d3"
      preset   = "16vcpu-64gb"
    }

  }

  current_region_defaults = local.regions_default[var.region]

  platform = coalesce(var.platform, local.current_region_defaults.platform)
  preset   = coalesce(var.preset, local.current_region_defaults.preset)

}
