test {
  parallel = true
}

mock_provider "nebius" {}
mock_provider "flux" {}
mock_provider "units" {}
mock_provider "string-functions" {}
mock_provider "kubernetes" {}
mock_provider "helm" {}

# GB300 workers are generated as fixed-size, rack-scoped NVLink node groups, so
# worker autoscaling must be rejected explicitly instead of silently ignored.
# Omitting autoscaling.enabled resolves to false for GB300 and true otherwise.

variables {
  active_checks_scope              = "essential"
  company_name                     = "gb300-autoscaling-test"
  slurm_login_ssh_root_public_keys = ["ssh-ed25519 test"]
  sizing_tier_override             = "S"
  nfs                              = { enabled = false }
  nfs_in_k8s                       = { enabled = false }
  region                           = "eu-north1"

  gb300_worker = {
    name                  = "gb300"
    size                  = 36
    resource              = { platform = "gpu-gb300", preset = "4gpu-112vcpu-800gb" }
    gpu_cluster           = { infiniband_fabric = "fabric-test" }
    nvlink                = { enabled = true }
    local_nvme            = { enabled = false }
    node_local_image_disk = { enabled = false }
  }
  cpu_worker = {
    name                  = "cpu"
    size                  = 1
    resource              = { platform = "cpu-d3", preset = "16vcpu-64gb" }
    node_local_image_disk = { enabled = false }
  }

  partitions = [
    {
      name     = "main"
      is_all   = true
      topology = "flat"
      config   = "Default=YES PriorityTier=10 PreemptMode=OFF MaxTime=INFINITE State=UP OverSubscribe=YES"
    },
    {
      name   = "hidden"
      is_all = true
      config = "Default=NO PriorityTier=10 PreemptMode=OFF Hidden=YES MaxTime=INFINITE State=UP OverSubscribe=YES"
    },
  ]
}

run "gb300_rejects_autoscaling_enabled" {
  command = plan
  variables {
    slurm_nodeset_workers     = [merge(var.gb300_worker, { autoscaling = { enabled = true, min_size = 0 } })]
    slurm_nodesets_partitions = var.partitions
  }
  plan_options {
    target = [module.slurm.helm_release.soperator_fluxcd_cm]
  }
  expect_failures = [var.slurm_nodeset_workers]
}

run "gb300_omitted_autoscaling_is_disabled" {
  command = plan
  variables {
    slurm_nodeset_workers     = [var.gb300_worker]
    slurm_nodesets_partitions = var.partitions
  }
  plan_options {
    target = [module.slurm.helm_release.soperator_fluxcd_cm]
  }
  assert {
    condition = (
      length(local.node_group_workers_v2) == 2 &&
      alltrue([for ng in local.node_group_workers_v2 : !ng.autoscaling && ng.min_size == 18 && ng.max_size == 18])
    )
    error_message = "GB300 with autoscaling omitted: expected two fixed 18-node groups with autoscaling off."
  }
}

run "gb300_accepts_autoscaling_disabled" {
  command = plan
  variables {
    slurm_nodeset_workers     = [merge(var.gb300_worker, { autoscaling = { enabled = false } })]
    slurm_nodesets_partitions = var.partitions
  }
  plan_options {
    target = [module.slurm.helm_release.soperator_fluxcd_cm]
  }
  assert {
    condition     = alltrue([for ng in local.node_group_workers_v2 : !ng.autoscaling])
    error_message = "GB300 with autoscaling.enabled = false: expected autoscaling off."
  }
}

run "non_gb300_omitted_autoscaling_is_enabled" {
  command = plan
  variables {
    slurm_nodeset_workers     = [var.cpu_worker]
    slurm_nodesets_partitions = var.partitions
  }
  plan_options {
    target = [module.slurm.helm_release.soperator_fluxcd_cm]
  }
  assert {
    condition     = alltrue([for ng in local.node_group_workers_v2 : ng.autoscaling])
    error_message = "Non-GB300 with autoscaling omitted: expected autoscaling to keep defaulting to enabled."
  }
}
