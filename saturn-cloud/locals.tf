locals {
  # Filestore
  filestore = {
    mount_tag  = "data"
    mount_path = var.filestore_mount_path
  }

  filesystem_csi_chart_name = "csi-mounted-fs-path"
  filesystem_csi_enabled    = local.shared_filesystem != null
  filesystem_csi_data_dir   = "${trimsuffix(local.filestore.mount_path, "/")}/csi-mounted-fs-path-data/"

  shared_filesystem = var.enable_filestore ? {
    id = try(
      one(nebius_compute_v1_filesystem.shared_filesystem).id,
      one(data.nebius_compute_v1_filesystem.shared_filesystem).id,
    )
    size_gibibytes = floor(provider::units::to_gib(try(
      one(nebius_compute_v1_filesystem.shared_filesystem).status.size_bytes,
      one(data.nebius_compute_v1_filesystem.shared_filesystem).status.size_bytes,
    )))
    mount_tag = local.filestore.mount_tag
  } : null

  ssh_public_key = var.ssh_public_key.key != null ? var.ssh_public_key.key : (
    try(fileexists(var.ssh_public_key.path), false) ? file(var.ssh_public_key.path) : null
  )

  # Derived URLs from saturn_domain
  saturn_base_url   = "https://app.${var.saturn_domain}"
  saturn_ssh_domain = "ssh.${var.saturn_domain}"

  # Hardcoded constants
  saturn_cloud_provider = "nebius"
  # Image builds schedule via node.saturncloud.io/role (atlas s2d/build.py), NOT the
  # chart's instanceConfig affinity. Workload pools no longer carry that label, but the
  # system pool does and is the same cpu-d3/4vcpu-16gb size builds used before.
  saturn_image_build_node_role = "system"

  default_cuda_preset = "cuda13.0"

  # ---------------------------------------------------------------------------
  # Region -> node pools
  # ---------------------------------------------------------------------------
  # Each Nebius region exposes a different set of GPU platforms. These pools MUST stay
  # in lock-step with the saturn-helm-operator-nebius chart's regionInstanceConfigs:
  # the chart surfaces an instance size per (platform, preset) here, scheduling on the
  # native labels node.kubernetes.io/instance-type + nebius.com/resource-preset, so a
  # missing node group means that size's pods pend forever. CPU (cpu-d3) is identical
  # everywhere; only the GPU platforms differ. 1-GPU presets only (8-GPU needs an
  # InfiniBand fabric, unsupported in the marketplace); L40S excluded (sold out).
  region_node_pools = {
    eu-north1 = [
      { platform = "cpu-d3", preset = "4vcpu-16gb" },
      { platform = "cpu-d3", preset = "16vcpu-64gb" },
      { platform = "cpu-d3", preset = "64vcpu-256gb" },
      { platform = "gpu-h200-sxm", preset = "1gpu-16vcpu-200gb" },
      { platform = "gpu-h100-sxm", preset = "1gpu-16vcpu-200gb" },
    ]
    eu-west1 = [
      { platform = "cpu-d3", preset = "4vcpu-16gb" },
      { platform = "cpu-d3", preset = "16vcpu-64gb" },
      { platform = "cpu-d3", preset = "64vcpu-256gb" },
      { platform = "gpu-h200-sxm", preset = "1gpu-16vcpu-200gb" },
    ]
    me-west1 = [
      { platform = "cpu-d3", preset = "4vcpu-16gb" },
      { platform = "cpu-d3", preset = "16vcpu-64gb" },
      { platform = "cpu-d3", preset = "64vcpu-256gb" },
      { platform = "gpu-b200-sxm-a", preset = "1gpu-20vcpu-224gb" },
    ]
    us-central1 = [
      { platform = "cpu-d3", preset = "4vcpu-16gb" },
      { platform = "cpu-d3", preset = "16vcpu-64gb" },
      { platform = "cpu-d3", preset = "64vcpu-256gb" },
      { platform = "gpu-b200-sxm", preset = "1gpu-20vcpu-224gb" },
      { platform = "gpu-h200-sxm", preset = "1gpu-16vcpu-200gb" },
      { platform = "gpu-rtx6000", preset = "1gpu-24vcpu-218gb" },
    ]
  }

  # Node pools come from the region table unless explicitly overridden via var.node_pools.
  # Each source is normalised to the SAME explicit object type by its own comprehension,
  # then selected — so the conditional's arms unify cleanly (no "inconsistent conditional
  # result types"). var.node_pools already carries optional-attr defaults; region-table
  # entries only set platform/preset, so the rest take module defaults here.
  override_node_pools = var.node_pools == null ? [] : [
    for pool in var.node_pools : {
      platform          = pool.platform
      preset            = pool.preset
      min_nodes         = pool.min_nodes
      max_nodes         = pool.max_nodes
      boot_disk_gb      = pool.boot_disk_gb
      infiniband_fabric = pool.infiniband_fabric
      drivers_preset    = pool.drivers_preset
    }
  ]

  region_default_node_pools = [
    for pool in lookup(local.region_node_pools, var.region, []) : {
      platform          = pool.platform
      preset            = pool.preset
      min_nodes         = 0
      max_nodes         = 10
      boot_disk_gb      = 372
      infiniband_fabric = null
      drivers_preset    = null
    }
  ]

  effective_node_pools = var.node_pools != null ? local.override_node_pools : local.region_default_node_pools

  # Build a node group key for each pool (used as for_each keys and node group names)
  node_pool_keys = {
    for i, pool in local.effective_node_pools : "${pool.platform}-${pool.preset}${pool.infiniband_fabric != null ? "-${pool.infiniband_fabric}" : ""}" => pool
  }

  # Derive unique infiniband fabrics from node pools that have fabric set
  # Keyed by "${platform}-${fabric}" to ensure one gpu_cluster per platform+fabric combo
  infiniband_fabrics = {
    for key, pool in local.node_pool_keys : "${pool.platform}-${pool.infiniband_fabric}" => pool
    if pool.infiniband_fabric != null
  }

  # NOTE: instanceConfig is no longer built here. The saturn-helm-operator-nebius chart
  # owns it (regionInstanceConfigs[region]); this module only provisions the matching
  # node groups. The region_node_pools table above is the shared contract.
}
