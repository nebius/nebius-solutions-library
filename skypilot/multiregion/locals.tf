locals {
  release-suffix = random_string.random.result
  ssh_public_key = var.ssh_public_key.key != null ? var.ssh_public_key.key : (
  fileexists(var.ssh_public_key.path) ? file(var.ssh_public_key.path) : null)

  filestore = {
    mount_tag  = "data"
    mount_path = "/mnt/data"
  }

  regions_default = {
    eu-west1 = {
      cpu_nodes_platform = "cpu-d3"
      cpu_nodes_preset   = "16vcpu-64gb"
      gpu_nodes_platform = "gpu-h200-sxm"
      gpu_nodes_preset   = "8gpu-128vcpu-1600gb"
      infiniband_fabric  = "fabric-5"
    }
    eu-north1 = {
      cpu_nodes_platform = "cpu-d3"
      cpu_nodes_preset   = "16vcpu-64gb"
      gpu_nodes_platform = "gpu-h100-sxm"
      gpu_nodes_preset   = "8gpu-128vcpu-1600gb"
      infiniband_fabric  = "fabric-3"
    }
    eu-north2 = {
      cpu_nodes_platform = "cpu-d3"
      cpu_nodes_preset   = "16vcpu-64gb"
      gpu_nodes_platform = "gpu-h200-sxm"
      gpu_nodes_preset   = "8gpu-128vcpu-1600gb"
      infiniband_fabric  = "eu-north2-a"
    }
    us-central1 = {
      cpu_nodes_platform = "cpu-d3"
      cpu_nodes_preset   = "16vcpu-64gb"
      gpu_nodes_platform = "gpu-h200-sxm"
      gpu_nodes_preset   = "8gpu-128vcpu-1600gb"
      infiniband_fabric  = "us-central1-a"
    }
    me-west1 = {
      cpu_nodes_platform = "cpu-d3"
      cpu_nodes_preset   = "16vcpu-64gb"
      gpu_nodes_platform = "gpu-b200-sxm-a"
      gpu_nodes_preset   = "8gpu-160vcpu-1792gb"
      infiniband_fabric  = "ramon"
    }
    uk-south1 = {
      cpu_nodes_platform = "cpu-d3"
      cpu_nodes_preset   = "16vcpu-64gb"
      gpu_nodes_platform = "gpu-b300-sxm"
      gpu_nodes_preset   = "8gpu-192vcpu-2768gb"
      infiniband_fabric  = "uk-south1-a"
    }
  }

  cluster_inputs = {
    region1 = {
      parent_id = coalesce(var.parent_id_region1, var.parent_id)
      subnet_id = coalesce(var.subnet_id_region1, var.subnet_id)
      region    = coalesce(var.region1, var.region)
    }
    region2 = {
      parent_id = var.parent_id_region2
      subnet_id = var.subnet_id_region2
      region    = var.region2
    }
  }

  deployment_clusters = {
    for cluster_key, cluster in local.cluster_inputs : cluster_key => cluster
    if cluster.parent_id != null && cluster.subnet_id != null && cluster.region != null && (
      cluster_key != "region2" || var.enable_secondary_region
    )
  }

  multi_region        = length(local.deployment_clusters) > 1
  primary-cluster-key = contains(keys(local.deployment_clusters), "region1") ? "region1" : keys(local.deployment_clusters)[0]
  primary_cluster     = local.deployment_clusters[local.primary-cluster-key]
  primary_parent_id   = local.primary_cluster.parent_id
  primary_region      = local.primary_cluster.region
  primary_subnet_id   = local.primary_cluster.subnet_id

  cluster_config = {
    for cluster_key, cluster in local.deployment_clusters : cluster_key => merge(cluster, {
      name               = local.multi_region ? "${var.cluster_name}-${cluster.region}" : var.cluster_name
      cpu_nodes_preset   = coalesce(var.cpu_nodes_preset, lookup(local.regions_default, cluster.region, local.regions_default["eu-north1"]).cpu_nodes_preset)
      cpu_nodes_platform = coalesce(var.cpu_nodes_platform, lookup(local.regions_default, cluster.region, local.regions_default["eu-north1"]).cpu_nodes_platform)
      gpu_nodes_platform = (
        cluster_key == "region1" ? coalesce(var.gpu_nodes_platform_primary, lookup(local.regions_default, cluster.region, local.regions_default["eu-north1"]).gpu_nodes_platform)
        : cluster_key == "region2" ? coalesce(var.gpu_nodes_platform_secondary, lookup(local.regions_default, cluster.region, local.regions_default["eu-north1"]).gpu_nodes_platform)
        : coalesce(var.gpu_nodes_platform, lookup(local.regions_default, cluster.region, local.regions_default["eu-north1"]).gpu_nodes_platform)
      )
      gpu_nodes_preset = (
        cluster_key == "region1" ? coalesce(var.gpu_nodes_preset_primary, lookup(local.regions_default, cluster.region, local.regions_default["eu-north1"]).gpu_nodes_preset)
        : cluster_key == "region2" ? coalesce(var.gpu_nodes_preset_secondary, lookup(local.regions_default, cluster.region, local.regions_default["eu-north1"]).gpu_nodes_preset)
        : coalesce(var.gpu_nodes_preset, lookup(local.regions_default, cluster.region, local.regions_default["eu-north1"]).gpu_nodes_preset)
      )
      infiniband_fabric = (
        cluster_key == "region1" ? coalesce(var.infiniband_fabric_primary, var.infiniband_fabric, lookup(local.regions_default, cluster.region, local.regions_default["eu-north1"]).infiniband_fabric)
        : cluster_key == "region2" ? coalesce(var.infiniband_fabric_secondary, var.infiniband_fabric, lookup(local.regions_default, cluster.region, local.regions_default["eu-north1"]).infiniband_fabric)
        : coalesce(var.infiniband_fabric, lookup(local.regions_default, cluster.region, local.regions_default["eu-north1"]).infiniband_fabric)
      )
      existing_filestore = cluster_key == "region1" ? (
        var.existing_filestore_region1 != null && trimspace(var.existing_filestore_region1) != "" ? var.existing_filestore_region1 : (
          var.existing_filestore != null && trimspace(var.existing_filestore) != "" ? var.existing_filestore : ""
        )
        ) : (
        var.existing_filestore_region2 != null && trimspace(var.existing_filestore_region2) != "" ? var.existing_filestore_region2 : ""
      )
    })
  }

  gpu_node_group_config = {
    for item in flatten([
      for cluster_key, cluster in local.cluster_config : [
        for group_index in range(var.gpu_node_groups) : {
          key         = "${cluster_key}-${group_index}"
          cluster_key = cluster_key
          group_index = group_index
          cluster     = cluster
        }
      ]
    ]) : item.key => item
  }

  cpu_nodes_preset   = local.cluster_config[local.primary-cluster-key].cpu_nodes_preset
  cpu_nodes_platform = local.cluster_config[local.primary-cluster-key].cpu_nodes_platform
  gpu_nodes_platform = local.cluster_config[local.primary-cluster-key].gpu_nodes_platform
  gpu_nodes_preset   = local.cluster_config[local.primary-cluster-key].gpu_nodes_preset
  infiniband_fabric  = local.cluster_config[local.primary-cluster-key].infiniband_fabric

  platform_to_cuda = {
    gpu-b200-sxm-a = "cuda12.8"
    gpu-b200-sxm   = "cuda12.8"
  }
  device_preset = lookup(local.platform_to_cuda, local.gpu_nodes_platform, "cuda13.0")

  valid_mig_parted_configs = {
    "gpu-h100-sxm"   = ["all-disabled", "all-enabled", "all-balanced", "all-1g.10gb", "all-1g.10gb.me", "all-1g.20gb", "all-2g.20gb", "all-3g.40gb", "all-4g.40gb", "all-7g.80gb"]
    "gpu-h200-sxm"   = ["all-disabled", "all-enabled", "all-balanced", "all-1g.18gb", "all-1g.18gb.me", "all-1g.35gb", "all-2g.35gb", "all-3g.71gb", "all-4g.71gb", "all-7g.141gb"]
    "gpu-b200-sxm"   = ["all-disabled", "all-enabled", "all-balanced", "all-1g.23gb", "all-1g.23gb.me", "all-1g.45gb", "all-2g.45gb", "all-3g.90gb", "all-4g.90gb", "all-7g.180gb"]
    "gpu-b200-sxm-a" = ["all-disabled", "all-enabled", "all-balanced", "all-1g.23gb", "all-1g.23gb.me", "all-1g.45gb", "all-2g.45gb", "all-3g.90gb", "all-4g.90gb", "all-7g.180gb"]
    "gpu-b300-sxm"   = ["all-disabled", "all-enabled", "all-balanced", "all-1g.23gb", "all-1g.23gb.me", "all-1g.45gb", "all-2g.45gb", "all-3g.90gb", "all-4g.90gb", "all-7g.180gb"]

  }

  # Mapping from platform and preset to hardware profile for nebius-gpu-health-checker
  platform_preset_to_hardware_profile = {
    # H100 configurations
    "gpu-h100-sxm-1gpu-16vcpu-200gb"   = "1xH100"
    "gpu-h100-sxm-8gpu-128vcpu-1600gb" = "8xH100"

    # H200 configurations
    "gpu-h200-sxm-1gpu-16vcpu-200gb"   = "1xH200"
    "gpu-h200-sxm-8gpu-128vcpu-1600gb" = "8xH200"

    # B200 configurations
    "gpu-b200-sxm-1gpu-20vcpu-224gb"     = "1xB200"
    "gpu-b200-sxm-8gpu-160vcpu-1792gb"   = "8xB200"
    "gpu-b200-sxm-a-8gpu-160vcpu-1792gb" = "8xB200"

    #B300 configuration
    "gpu-b300-sxm-8gpu-192vcpu-2768gb" = "8xB300"
    "gpu-b300-sxm-1gpu-24vcpu-346gb"   = "1xB300"

    # L40 configurations
    "gpu-l40s-d-1gpu-16vcpu-96gb"    = "1xL40S"
    "gpu-l40s-d-1gpu-32vcpu-192gb"   = "1xL40S"
    "gpu-l40s-d-1gpu-48vcpu-288gb"   = "1xL40S"
    "gpu-l40s-d-2gpu-64vcpu-384gb"   = "2xL40S"
    "gpu-l40s-d-2gpu-64vcpu-384gb"   = "2xL40S"
    "gpu-l40s-d-2gpu-96vcpu-576gb"   = "2xL40S"
    "gpu-l40s-d-4gpu-128vcpu-768gb"  = "4xL40S"
    "gpu-l40s-d-4gpu-192vcpu-1152gb" = "4xL40S"
    "gpu-l40s-a-1gpu-8vcpu-32gb"     = "1XL40S"
    "gpu-l40s-a-1gpu-24vcpu-96gb"    = "1X40S"
    "gpu-l40s-a-1gpu-32vcpu-128gb"   = "1X40S"
    "gpu-l40s-a-1gpu-40vcpu-160gb"   = "1X40S"
  }

  # Create the key for hardware profile lookup
  hardware_profile_key = "${local.gpu_nodes_platform}-${local.gpu_nodes_preset}"
}

resource "random_string" "random" {
  keepers = {
    ami_id = local.primary_parent_id
  }
  length  = 6
  upper   = true
  lower   = true
  numeric = true
  special = false
}
