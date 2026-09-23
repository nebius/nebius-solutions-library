locals {
  release-suffix = random_string.random.result
  ssh_public_key = var.ssh_public_key.key != null ? var.ssh_public_key.key : (
  fileexists(var.ssh_public_key.path) ? file(var.ssh_public_key.path) : null)

  filestore = {
    mount_tag  = "data"
    mount_path = var.filestore_mount_path
  }

  filesystem_csi_chart_name          = "csi-mounted-fs-path"
  filesystem_csi_storage_class_name  = "csi-mounted-fs-path-sc"
  filesystem_csi_enabled             = local.shared-filesystem != null
  filesystem_csi_data_dir            = "${trimsuffix(local.filestore.mount_path, "/")}/csi-mounted-fs-path-data/"
  filesystem_csi_previous_default_sc = var.filesystem_csi.previous_default_storage_class_name

  regions_default = {
    eu-west1 = {
      cpu_nodes_platform = "cpu-d3"
      cpu_nodes_preset   = "16vcpu-64gb"
      gpu_nodes_platform = "gpu-h200-sxm"
      gpu_nodes_preset   = "8gpu-128vcpu-1600gb"
    }
    eu-north1 = {
      cpu_nodes_platform = "cpu-d3"
      cpu_nodes_preset   = "16vcpu-64gb"
      gpu_nodes_platform = "gpu-h100-sxm"
      gpu_nodes_preset   = "8gpu-128vcpu-1600gb"
    }
    eu-north2 = {
      cpu_nodes_platform = "cpu-d3"
      cpu_nodes_preset   = "16vcpu-64gb"
      gpu_nodes_platform = "gpu-h200-sxm"
      gpu_nodes_preset   = "8gpu-128vcpu-1600gb"
    }
    us-central1 = {
      cpu_nodes_platform = "cpu-d3"
      cpu_nodes_preset   = "16vcpu-64gb"
      gpu_nodes_platform = "gpu-h200-sxm"
      gpu_nodes_preset   = "8gpu-128vcpu-1600gb"
    }
    me-west1 = {
      cpu_nodes_platform = "cpu-d3"
      cpu_nodes_preset   = "16vcpu-64gb"
      gpu_nodes_platform = "gpu-b200-sxm-a"
      gpu_nodes_preset   = "8gpu-160vcpu-1792gb"
    }
    uk-south1 = {
      cpu_nodes_platform = "cpu-d3"
      cpu_nodes_preset   = "16vcpu-64gb"
      gpu_nodes_platform = "gpu-b300-sxm"
      gpu_nodes_preset   = "8gpu-192vcpu-2768gb"
    }
    eu-west2 = {
      cpu_nodes_platform = "cpu-d3"
      cpu_nodes_preset   = "16vcpu-64gb"
      gpu_nodes_platform = "gpu-b300-sxm"
      gpu_nodes_preset   = "8gpu-192vcpu-2768gb"
    }
    us-north1 = {
      cpu_nodes_platform = "cpu-d3"
      cpu_nodes_preset   = "16vcpu-64gb"
      gpu_nodes_platform = "gpu-b300-sxm"
      gpu_nodes_preset   = "8gpu-192vcpu-2768gb"
    }
  }

  current_region_defaults = local.regions_default[var.region]

  cpu_nodes_preset     = coalesce(var.cpu_nodes_preset, local.current_region_defaults.cpu_nodes_preset)
  cpu_nodes_platform   = coalesce(var.cpu_nodes_platform, local.current_region_defaults.cpu_nodes_platform)
  gpu_nodes_platform   = coalesce(var.gpu_nodes_platform, local.current_region_defaults.gpu_nodes_platform)
  gpu_nodes_preset     = coalesce(var.gpu_nodes_preset, local.current_region_defaults.gpu_nodes_preset)
  infiniband_fabric    = var.infiniband_fabric
  enable_gpu_cluster   = local.infiniband_fabric != null ? trimspace(local.infiniband_fabric) != "" : false
  device_preset        = "cuda13.0"
  gb300_nodes_per_rack = 18
  gb300_gpus_per_node  = 4
  gb300_rack_count     = floor(max(0, var.gb300.rack_count))
  gb300_enabled        = local.gb300_rack_count > 0
  gb300_platform       = "gpu-gb300"
  gb300_preset         = "4gpu-112vcpu-800gb"
  gb300_racks = {
    for index in range(local.gb300_rack_count) :
    format("rack-%03d", index + 1) => {
      node_count = local.gb300_nodes_per_rack
    }
  }
  use_driverfull_gpu = var.gpu_nodes_driverfull_image || local.gb300_enabled
  gpu_operator_cdi_enabled = (
    !local.use_driverfull_gpu &&
    var.mig_strategy != null &&
    var.mig_strategy != "none"
  ) ? true : null

  # Known-good kubelet NUMA/topology configs validated during GPU node testing.
  gpu_kubelet_numa_presets = {
    "h200-standard" = {
      cpu_manager_policy      = "static"
      topology_manager_policy = "restricted"
      memory_manager_policy   = "Static"
      kube_reserved_memory    = "1229Mi"
      reserved_memory = [
        {
          numa_node = 0
          memory    = "1329Mi"
        }
      ]
    }
    "b200-standard" = {
      cpu_manager_policy      = "static"
      topology_manager_policy = "restricted"
      memory_manager_policy   = "Static"
      kube_reserved_memory    = "1229Mi"
      reserved_memory = [
        {
          numa_node = 0
          memory    = "1329Mi"
        }
      ]
    }
    "b300-standard" = {
      cpu_manager_policy      = "static"
      topology_manager_policy = "restricted"
      memory_manager_policy   = "Static"
      kube_reserved_memory    = "1229Mi"
      reserved_memory = [
        {
          numa_node = 0
          memory    = "1329Mi"
        }
      ]
    }
  }

  gpu_kubelet_numa_platform_presets = {
    "gpu-h200-sxm"   = "h200-standard"
    "gpu-b200-sxm"   = "b200-standard"
    "gpu-b200-sxm-a" = "b200-standard"
    "gpu-b300-sxm"   = "b300-standard"
  }

  resolved_gpu_kubelet_numa_preset = var.gpu_kubelet_numa_preset != null ? var.gpu_kubelet_numa_preset : (
    var.enable_gpu_kubelet_numa ? lookup(
      local.gpu_kubelet_numa_platform_presets,
      local.gpu_nodes_platform,
      null
    ) : null
  )

  effective_gpu_kubelet_numa_config = var.gpu_kubelet_numa_config != null ? var.gpu_kubelet_numa_config : (
    local.resolved_gpu_kubelet_numa_preset == null ? null : local.gpu_kubelet_numa_presets[local.resolved_gpu_kubelet_numa_preset]
  )

  gpu_kubelet_numa_config_yaml = local.effective_gpu_kubelet_numa_config == null ? "" : yamlencode(
    merge(
      {
        cpuManagerPolicy      = local.effective_gpu_kubelet_numa_config.cpu_manager_policy
        topologyManagerPolicy = local.effective_gpu_kubelet_numa_config.topology_manager_policy
        memoryManagerPolicy   = local.effective_gpu_kubelet_numa_config.memory_manager_policy
        reservedMemory = [
          for item in local.effective_gpu_kubelet_numa_config.reserved_memory : {
            numaNode = item.numa_node
            limits = {
              memory = item.memory
            }
          }
        ]
      },
      local.effective_gpu_kubelet_numa_config.kube_reserved_memory == null ? {} : {
        kubeReserved = {
          memory = local.effective_gpu_kubelet_numa_config.kube_reserved_memory
        }
      }
    )
  )

  #List of official MIG configs https://docs.nvidia.com/datacenter/tesla/mig-user-guide/supported-mig-profiles.html
  valid_mig_parted_configs = {
    "gpu-h100-sxm"   = ["all-disabled", "all-enabled", "all-balanced", "all-1g.10gb", "all-1g.10gb.me", "all-1g.20gb", "all-2g.20gb", "all-3g.40gb", "all-4g.40gb", "all-7g.80gb"]
    "gpu-h200-sxm"   = ["all-disabled", "all-enabled", "all-balanced", "all-1g.18gb", "all-1g.18gb.me", "all-1g.35gb", "all-2g.35gb", "all-3g.71gb", "all-4g.71gb", "all-7g.141gb"]
    "gpu-b200-sxm"   = ["all-disabled", "all-enabled", "all-balanced", "all-1g.23gb", "all-1g.23gb.me", "all-1g.45gb", "all-2g.45gb", "all-3g.90gb", "all-4g.90gb", "all-7g.180gb"]
    "gpu-b200-sxm-a" = ["all-disabled", "all-enabled", "all-balanced", "all-1g.23gb", "all-1g.23gb.me", "all-1g.45gb", "all-2g.45gb", "all-3g.90gb", "all-4g.90gb", "all-7g.180gb"]
    "gpu-b300-sxm"   = ["all-disabled", "all-enabled", "all-balanced", "all-1g.23gb", "all-1g.23gb.me", "all-1g.45gb", "all-2g.45gb", "all-3g.90gb", "all-4g.90gb", "all-7g.180gb"]
    "gpu-rtx6000"    = ["all-disabled", "all-enabled", "all-balanced", "all-1g.24gb", "all-1g.24gb.me", "all-1g.48gb", "all-2g.48gb", "all-4g.96gb"]
  }

  # Updated: 2026/06/15 should be updated regularly
  binpacking_kube_sched_ver_by_k8s_minor = {
    "30" = "1.30.14"
    "31" = "1.31.14"
    "32" = "1.32.13"
    "33" = "1.33.13"
    "34" = "1.34.9"
  }

  # Logic here:
  # if we define a kube scheduler version it overrides all (must include patch version)
  # else if k8s_version is defined and it include a patch version we use it
  # else we use the minor version to lookup a reasonable patch version
  # if none are defined we are in an error state picked up by the lifecycle precondition
  binpacking_k8s_semver = var.k8s_version == null ? null : split(".", replace(tostring(var.k8s_version), "/^v?([0-9]+\\.[0-9]+(?:\\.[0-9]+)?$)/", "$1"))
  binpacking_k8s_minor  = try(local.binpacking_k8s_semver[1], null)
  binpacking_k8s_patch  = try(local.binpacking_k8s_semver[2], null)

  binpacking_kube_sched_ver = try(coalesce(
    var.binpacking_kube_sched_ver,
    local.binpacking_k8s_patch == null ? null : join(".", local.binpacking_k8s_semver),
    try(lookup(local.binpacking_kube_sched_ver_by_k8s_minor, local.binpacking_k8s_minor, null), null)
  ), null)
  binpacking_enable_mutator = try(length(var.binpacking_forced_namespaces), 0) > 0 ? true : false
}

resource "random_string" "random" {
  keepers = {
    ami_id = "${var.parent_id}"
  }
  length  = 6
  upper   = true
  lower   = true
  numeric = true
  special = false
}
