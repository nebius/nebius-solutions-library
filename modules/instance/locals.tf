locals {

  users = [
    for user in var.users : {
      user_name = user.user_name
      ssh_public_key = user.ssh_public_key != null ? user.ssh_public_key : (
      fileexists(user.ssh_key_path) ? file(user.ssh_key_path) : null)
    }
  ]

  regions_default = {
    eu-west1 = {
      cpu_nodes_platform = "cpu-d3"
      cpu_nodes_preset   = "16vcpu-64gb"
      gpu_nodes_platform = "gpu-h200-sxm"
      gpu_nodes_preset   = "1gpu-16vcpu-200gb"
    }
    eu-north1 = {
      cpu_nodes_platform = "cpu-d3"
      cpu_nodes_preset   = "16vcpu-64gb"
      gpu_nodes_platform = "gpu-h100-sxm"
      gpu_nodes_preset   = "1gpu-16vcpu-200gb"
    }
    eu-west2 = {
      cpu_nodes_platform = "cpu-d3"
      cpu_nodes_preset   = "16vcpu-64gb"
      gpu_nodes_platform = "gpu-b300-sxm"
      gpu_nodes_preset   = "1gpu-24vcpu-346gb"
    }
  }

  current_region_defaults = local.regions_default[var.region]

  boot_image_family = coalesce(
    var.boot_image_family,
    var.platform == "gpu-b300-sxm" ? "ubuntu24.04-cuda13.0" : "ubuntu24.04-cuda12"
  )

  # cpu_nodes_preset   = coalesce(var.cpu_nodes_preset, local.current_region_defaults.cpu_nodes_preset)
  # cpu_nodes_platform = coalesce(var.cpu_nodes_platform, local.current_region_defaults.cpu_nodes_platform)
  # gpu_nodes_platform = coalesce(var.gpu_nodes_platform, local.current_region_defaults.gpu_nodes_platform)
  # gpu_nodes_preset   = coalesce(var.gpu_nodes_preset, local.current_region_defaults.gpu_nodes_preset)

  extra_path    = var.extra_path
  extra_disk_id = var.add_extra_storage ? substr(nebius_compute_v1_disk.extra-storage-disk[0].id, 0, 20) : ""


  cloud_init_log = jsonencode({
    extra_path    = local.extra_path
    extra_disk_id = local.extra_disk_id
    state         = terraform.workspace
    users         = local.users

  })
  # current_region_defaults = local.regions_default[var.region]
  #
  # cpu_nodes_preset   = coalesce(var.cpu_nodes_preset, local.current_region_defaults.cpu_nodes_preset)
  # cpu_nodes_platform = coalesce(var.cpu_nodes_platform, local.current_region_defaults.cpu_nodes_platform)
}
