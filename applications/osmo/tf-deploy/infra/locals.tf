# =============================================================================
# Local Values
# =============================================================================

locals {
  # Resource naming
  name_prefix = "${var.project_name}-${var.environment}"

  # SSH key handling
  ssh_public_key = (
    try(trimspace(var.ssh_public_key.key), "") != ""
    ? trimspace(var.ssh_public_key.key)
    : try(trimspace(var.ssh_public_key.path), "") != "" && fileexists(trimspace(var.ssh_public_key.path))
    ? file(trimspace(var.ssh_public_key.path))
    : null
  )

  # Region-specific defaults
  region_defaults = {
    eu-north1 = {
      gpu_nodes_platform   = "gpu-h100-sxm"
      gpu_nodes_preset     = "8gpu-128vcpu-1600gb"
      infiniband_fabric    = "fabric-3"
      postgresql_platform  = "cpu-e2"
      postgresql_disk_type = "network-ssd"
    }
    eu-north2 = {
      gpu_nodes_platform   = "gpu-h200-sxm"
      gpu_nodes_preset     = "8gpu-128vcpu-1600gb"
      infiniband_fabric    = "eu-north2-a"
      postgresql_platform  = "cpu-d3"
      postgresql_disk_type = "network-ssd"
    }
    eu-west1 = {
      gpu_nodes_platform   = "gpu-h200-sxm"
      gpu_nodes_preset     = "8gpu-128vcpu-1600gb"
      infiniband_fabric    = "fabric-5"
      postgresql_platform  = "cpu-d3"
      postgresql_disk_type = "network-ssd"
    }
    me-west1 = {
      gpu_nodes_platform   = "gpu-b200-sxm-a"
      gpu_nodes_preset     = "8gpu-160vcpu-1792gb"
      infiniband_fabric    = "me-west1-a"
      postgresql_platform  = "cpu-d3"
      postgresql_disk_type = "network-ssd"
    }
    uk-south1 = {
      gpu_nodes_platform   = "gpu-b300-sxm"
      gpu_nodes_preset     = "8gpu-192vcpu-2768gb"
      infiniband_fabric    = "uk-south1-a"
      postgresql_platform  = "cpu-d3"
      postgresql_disk_type = "network-ssd"
    }
    us-central1 = {
      gpu_nodes_platform   = "gpu-h200-sxm"
      gpu_nodes_preset     = "8gpu-128vcpu-1600gb"
      infiniband_fabric    = "us-central1-a"
      postgresql_platform  = "cpu-d3"
      postgresql_disk_type = "network-ssd"
    }
  }

  # Available GPU platforms by region (for reference)
  #
  # eu-north1:
  #   - gpu-h100-sxm   (NVIDIA H100 80GB HBM3)    presets: 1gpu-16vcpu-200gb, 8gpu-128vcpu-1600gb
  #   - gpu-h200-sxm   (NVIDIA H200)               presets: 1gpu-16vcpu-200gb, 8gpu-128vcpu-1600gb
  #   - gpu-l40s-a     (L40S Intel, 48GB VRAM)      presets: 1gpu-8vcpu-32gb, 2gpu-16vcpu-64gb
  #   - gpu-l40s-d     (L40S AMD, 48GB VRAM)        presets: 1gpu-8vcpu-32gb, 2gpu-16vcpu-64gb
  #
  # eu-north2:
  #   - gpu-h200-sxm   (NVIDIA H200)               presets: 1gpu-16vcpu-200gb, 8gpu-128vcpu-1600gb
  #
  # eu-west1:
  #   - gpu-h200-sxm   (NVIDIA H200)               presets: 1gpu-16vcpu-200gb, 8gpu-128vcpu-1600gb
  #
  # me-west1:
  #   - gpu-b200-sxm-a (NVIDIA B200)               presets: 1gpu-20vcpu-224gb, 8gpu-160vcpu-1792gb
  #
  # uk-south1:
  #   - gpu-b300-sxm   (NVIDIA B300 SXM6 AC)       presets: 1gpu-24vcpu-346gb, 8gpu-192vcpu-2768gb
  #
  # us-central1:
  #   - gpu-h200-sxm   (NVIDIA H200)               presets: 1gpu-16vcpu-200gb, 8gpu-128vcpu-1600gb
  #   - gpu-b200-sxm   (NVIDIA B200)               presets: 1gpu-20vcpu-224gb, 8gpu-160vcpu-1792gb

  # Current region config with overrides
  current_region = local.region_defaults[var.region]

  gpu_nodes_platform = coalesce(var.gpu_nodes_platform, local.current_region.gpu_nodes_platform)
  gpu_nodes_preset   = coalesce(var.gpu_nodes_preset, local.current_region.gpu_nodes_preset)
  infiniband_fabric  = coalesce(var.infiniband_fabric, local.current_region.infiniband_fabric)

  postgresql_platform  = coalesce(var.postgresql_platform, local.current_region.postgresql_platform)
  postgresql_disk_type = coalesce(var.postgresql_disk_type, local.current_region.postgresql_disk_type)

  # Driverfull image: map GPU platform to CUDA driver preset
  platform_to_cuda = {
    gpu-b200-sxm-a = "cuda12.8"
    gpu-b200-sxm   = "cuda12.8"
    gpu-b300-sxm   = "cuda13.0"
  }
  gpu_drivers_preset = lookup(local.platform_to_cuda, local.gpu_nodes_platform, "cuda12")

  # Generate unique storage bucket name if not provided
  storage_bucket_name = var.storage_bucket_name != "" ? var.storage_bucket_name : "${local.name_prefix}-storage-${random_string.suffix.result}"

  # Common tags/labels
  common_labels = {
    project     = var.project_name
    environment = var.environment
    managed_by  = "terraform"
  }
}

# Random suffix for unique naming
resource "random_string" "suffix" {
  length  = 8
  lower   = true
  upper   = false
  numeric = true
  special = false

  keepers = {
    project_id = var.parent_id
  }
}
