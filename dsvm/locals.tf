locals {
  ssh_public_key = var.ssh_public_key.key != null ? var.ssh_public_key.key : (
  fileexists(var.ssh_public_key.path) ? file(var.ssh_public_key.path) : null)

  region_default_platforms = {
    eu-north1   = "gpu-h100-sxm"
    eu-north2   = "gpu-h200-sxm"
    eu-west1    = "gpu-h200-sxm"
    eu-west2    = "gpu-b300-sxm"
    me-west1    = "gpu-b200-sxm-a"
    uk-south1   = "gpu-b300-sxm"
    us-central1 = "gpu-h200-sxm"
    us-north1   = "gpu-b300-sxm"
  }

  platform_defaults_by_platform = {
    gpu-h100-sxm = {
      preset            = "1gpu-16vcpu-200gb"
      boot_image_family = "ubuntu24.04-cuda12"
    }
    gpu-h200-sxm = {
      preset            = "1gpu-16vcpu-200gb"
      boot_image_family = "ubuntu24.04-cuda12"
    }
    gpu-b200-sxm-a = {
      preset            = "1gpu-20vcpu-224gb"
      boot_image_family = "ubuntu24.04-cuda13.0"
    }
    gpu-b300-sxm = {
      preset            = "1gpu-24vcpu-346gb"
      boot_image_family = "ubuntu24.04-cuda13.0"
    }
  }

  platform          = coalesce(var.platform, try(local.region_default_platforms[var.region], null))
  preset            = coalesce(var.preset, try(local.platform_defaults_by_platform[local.platform].preset, null))
  boot_image_family = coalesce(var.boot_image_family, try(local.platform_defaults_by_platform[local.platform].boot_image_family, null))
}
