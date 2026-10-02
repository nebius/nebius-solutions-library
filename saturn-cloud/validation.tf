locals {
  # Map of all valid platform -> preset combinations with metadata.
  # Used for validation and to auto-generate saturn_instance_config.
  valid_presets = {
    ############################
    # cpu-d3
    ############################
    "cpu-d3/4vcpu-16gb"     = { cores = 4, memory = "16Gi", gpus = 0, gpu_type = null, hardware_type = null }
    "cpu-d3/8vcpu-32gb"     = { cores = 8, memory = "32Gi", gpus = 0, gpu_type = null, hardware_type = null }
    "cpu-d3/16vcpu-64gb"    = { cores = 16, memory = "64Gi", gpus = 0, gpu_type = null, hardware_type = null }
    "cpu-d3/32vcpu-128gb"   = { cores = 32, memory = "128Gi", gpus = 0, gpu_type = null, hardware_type = null }
    "cpu-d3/48vcpu-192gb"   = { cores = 48, memory = "192Gi", gpus = 0, gpu_type = null, hardware_type = null }
    "cpu-d3/64vcpu-256gb"   = { cores = 64, memory = "256Gi", gpus = 0, gpu_type = null, hardware_type = null }
    "cpu-d3/80vcpu-320gb"   = { cores = 80, memory = "320Gi", gpus = 0, gpu_type = null, hardware_type = null }
    "cpu-d3/96vcpu-384gb"   = { cores = 96, memory = "384Gi", gpus = 0, gpu_type = null, hardware_type = null }
    "cpu-d3/128vcpu-512gb"  = { cores = 128, memory = "512Gi", gpus = 0, gpu_type = null, hardware_type = null }
    "cpu-d3/160vcpu-640gb"  = { cores = 160, memory = "640Gi", gpus = 0, gpu_type = null, hardware_type = null }
    "cpu-d3/192vcpu-768gb"  = { cores = 192, memory = "768Gi", gpus = 0, gpu_type = null, hardware_type = null }
    "cpu-d3/256vcpu-1024gb" = { cores = 256, memory = "1024Gi", gpus = 0, gpu_type = null, hardware_type = null }

    ############################
    # cpu-e2
    ############################
    "cpu-e2/2vcpu-8gb"    = { cores = 2, memory = "8Gi", gpus = 0, gpu_type = null, hardware_type = null }
    "cpu-e2/4vcpu-16gb"   = { cores = 4, memory = "16Gi", gpus = 0, gpu_type = null, hardware_type = null }
    "cpu-e2/8vcpu-32gb"   = { cores = 8, memory = "32Gi", gpus = 0, gpu_type = null, hardware_type = null }
    "cpu-e2/16vcpu-64gb"  = { cores = 16, memory = "64Gi", gpus = 0, gpu_type = null, hardware_type = null }
    "cpu-e2/32vcpu-128gb" = { cores = 32, memory = "128Gi", gpus = 0, gpu_type = null, hardware_type = null }
    "cpu-e2/48vcpu-192gb" = { cores = 48, memory = "192Gi", gpus = 0, gpu_type = null, hardware_type = null }
    "cpu-e2/64vcpu-256gb" = { cores = 64, memory = "256Gi", gpus = 0, gpu_type = null, hardware_type = null }
    "cpu-e2/80vcpu-320gb" = { cores = 80, memory = "320Gi", gpus = 0, gpu_type = null, hardware_type = null }

    ############################
    # gpu-h100-sxm
    ############################
    "gpu-h100-sxm/1gpu-16vcpu-200gb"   = { cores = 16, memory = "200Gi", gpus = 1, gpu_type = "H100", hardware_type = "NVIDIA" }
    "gpu-h100-sxm/8gpu-128vcpu-1600gb" = { cores = 128, memory = "1600Gi", gpus = 8, gpu_type = "H100", hardware_type = "NVIDIA" }

    ############################
    # gpu-h200-sxm
    ############################
    "gpu-h200-sxm/1gpu-16vcpu-200gb"   = { cores = 16, memory = "200Gi", gpus = 1, gpu_type = "H200", hardware_type = "NVIDIA" }
    "gpu-h200-sxm/8gpu-128vcpu-1600gb" = { cores = 128, memory = "1600Gi", gpus = 8, gpu_type = "H200", hardware_type = "NVIDIA" }

    ############################
    # gpu-b200-sxm
    ############################
    "gpu-b200-sxm/1gpu-20vcpu-224gb"   = { cores = 20, memory = "224Gi", gpus = 1, gpu_type = "B200", hardware_type = "NVIDIA" }
    "gpu-b200-sxm/8gpu-160vcpu-1792gb" = { cores = 160, memory = "1792Gi", gpus = 8, gpu_type = "B200", hardware_type = "NVIDIA" }

    ############################
    # gpu-b200-sxm-a
    ############################
    "gpu-b200-sxm-a/1gpu-20vcpu-224gb"   = { cores = 20, memory = "224Gi", gpus = 1, gpu_type = "B200", hardware_type = "NVIDIA" }
    "gpu-b200-sxm-a/8gpu-160vcpu-1792gb" = { cores = 160, memory = "1792Gi", gpus = 8, gpu_type = "B200", hardware_type = "NVIDIA" }

    ############################
    # gpu-b300-sxm
    ############################
    "gpu-b300-sxm/1gpu-24vcpu-346gb"   = { cores = 24, memory = "346Gi", gpus = 1, gpu_type = "B300", hardware_type = "NVIDIA" }
    "gpu-b300-sxm/8gpu-192vcpu-2768gb" = { cores = 192, memory = "2768Gi", gpus = 8, gpu_type = "B300", hardware_type = "NVIDIA" }

    ############################
    # gpu-rtx6000 (RTX PRO 6000)
    ############################
    "gpu-rtx6000/1gpu-24vcpu-218gb" = { cores = 24, memory = "218Gi", gpus = 1, gpu_type = "RTX6000", hardware_type = "NVIDIA" }

    ############################
    # gpu-l40s-a
    ############################
    "gpu-l40s-a/1gpu-8vcpu-32gb"   = { cores = 8, memory = "32Gi", gpus = 1, gpu_type = "L40S", hardware_type = "NVIDIA" }
    "gpu-l40s-a/1gpu-12vcpu-48gb"  = { cores = 12, memory = "48Gi", gpus = 1, gpu_type = "L40S", hardware_type = "NVIDIA" }
    "gpu-l40s-a/1gpu-16vcpu-64gb"  = { cores = 16, memory = "64Gi", gpus = 1, gpu_type = "L40S", hardware_type = "NVIDIA" }
    "gpu-l40s-a/1gpu-24vcpu-96gb"  = { cores = 24, memory = "96Gi", gpus = 1, gpu_type = "L40S", hardware_type = "NVIDIA" }
    "gpu-l40s-a/1gpu-40vcpu-160gb" = { cores = 40, memory = "160Gi", gpus = 1, gpu_type = "L40S", hardware_type = "NVIDIA" }

    ############################
    # gpu-l40s-d
    ############################
    "gpu-l40s-d/1gpu-16vcpu-96gb"    = { cores = 16, memory = "96Gi", gpus = 1, gpu_type = "L40S", hardware_type = "NVIDIA" }
    "gpu-l40s-d/1gpu-24vcpu-144gb"   = { cores = 24, memory = "144Gi", gpus = 1, gpu_type = "L40S", hardware_type = "NVIDIA" }
    "gpu-l40s-d/1gpu-48vcpu-288gb"   = { cores = 48, memory = "288Gi", gpus = 1, gpu_type = "L40S", hardware_type = "NVIDIA" }
    "gpu-l40s-d/2gpu-48vcpu-288gb"   = { cores = 48, memory = "288Gi", gpus = 2, gpu_type = "L40S", hardware_type = "NVIDIA" }
    "gpu-l40s-d/2gpu-96vcpu-576gb"   = { cores = 96, memory = "576Gi", gpus = 2, gpu_type = "L40S", hardware_type = "NVIDIA" }
    "gpu-l40s-d/4gpu-96vcpu-576gb"   = { cores = 96, memory = "576Gi", gpus = 4, gpu_type = "L40S", hardware_type = "NVIDIA" }
    "gpu-l40s-d/4gpu-192vcpu-1152gb" = { cores = 192, memory = "1152Gi", gpus = 4, gpu_type = "L40S", hardware_type = "NVIDIA" }
  }

  # Extract the set of valid platforms from the preset map
  valid_platforms = toset([for key, _ in local.valid_presets : split("/", key)[0]])
}

############################
# Validation checks
############################

# 0. When node_pools is not overridden, region must be one with a known pool set
resource "terraform_data" "validate_region" {
  lifecycle {
    precondition {
      condition     = var.node_pools != null || contains(keys(local.region_node_pools), var.region)
      error_message = "Region '${var.region}' has no default node pools. Supported: ${join(", ", keys(local.region_node_pools))}. Override var.node_pools to use another region."
    }
  }
}

# 1. Each node pool's platform must be valid
resource "terraform_data" "validate_platforms" {
  for_each = { for i, pool in local.effective_node_pools : i => pool }

  lifecycle {
    precondition {
      condition     = contains(keys(local.valid_presets), "${each.value.platform}/${each.value.preset}")
      error_message = "Node pool ${each.key}: platform '${each.value.platform}' with preset '${each.value.preset}' is not a valid combination. Check validation.tf for valid options."
    }
  }
}

# 2. 8-GPU presets require infiniband_fabric
resource "terraform_data" "validate_infiniband" {
  for_each = { for i, pool in local.effective_node_pools : i => pool }

  lifecycle {
    precondition {
      condition     = !(startswith(each.value.preset, "8gpu-") && each.value.infiniband_fabric == null)
      error_message = "Node pool ${each.key}: 8-GPU preset '${each.value.preset}' requires infiniband_fabric to be set."
    }
  }
}

# 3. No duplicate platform+preset+fabric combinations
resource "terraform_data" "validate_no_duplicates" {
  lifecycle {
    precondition {
      condition = length(local.effective_node_pools) == length(toset([
        for pool in local.effective_node_pools : "${pool.platform}-${pool.preset}${pool.infiniband_fabric != null ? "-${pool.infiniband_fabric}" : ""}"
      ]))
      error_message = "Duplicate node pool detected. Each platform+preset+fabric combination must be unique."
    }
  }
}
