test {
  parallel = true
}

mock_provider "nebius" {}
mock_provider "flux" {}
mock_provider "units" {}
mock_provider "string-functions" {}
mock_provider "kubernetes" {}
mock_provider "helm" {}

# auto_suspend_seconds renders nodeConfig.autoSuspendSeconds, which soperator turns into
# SuspendTime on the NodeSet's NodeName line. 0 is a meaningful value (suspend idle nodes
# immediately) and must not be dropped; null must omit the key so the partition-level and
# global SuspendTime keep applying.

variables {
  active_checks_scope              = "essential"
  company_name                     = "auto-suspend-test"
  slurm_login_ssh_root_public_keys = ["ssh-ed25519 test"]
  sizing_tier_override             = "S"
  nfs                              = { enabled = false }
  nfs_in_k8s                       = { enabled = false }

  cpu_worker = {
    name                      = "cpu"
    size                      = 1
    resource                  = { platform = "cpu-d3", preset = "16vcpu-64gb" }
    boot_disk                 = { type = "NETWORK_SSD", size_gibibytes = 128, block_size_kibibytes = 4 }
    node_local_image_disk     = { enabled = false }
    node_local_jail_submounts = []
  }

  slurm_nodesets_partitions = [
    {
      name               = "main"
      is_all             = true
      slurm_nodeset_refs = []
      topology           = "flat"
      config             = "Default=YES PriorityTier=10 PreemptMode=OFF MaxTime=INFINITE State=UP OverSubscribe=YES"
    },
    {
      name   = "hidden"
      is_all = true
      config = "Default=NO PriorityTier=10 PreemptMode=OFF Hidden=YES MaxTime=INFINITE State=UP OverSubscribe=YES"
    },
  ]
}

run "auto_suspend_seconds_is_omitted_by_default" {
  command = plan

  variables {
    slurm_nodeset_workers = [var.cpu_worker]
  }

  assert {
    condition = [
      for nodeset in yamldecode(module.slurm.debug_flux_release_nodesets_rendered).nodesets.overrideValues.nodesets :
      try(nodeset.nodeConfig.autoSuspendSeconds, null)
    ] == [null]
    error_message = "An unset auto_suspend_seconds must not render nodeConfig.autoSuspendSeconds."
  }
}

run "auto_suspend_seconds_renders_zero_and_positive_values" {
  command = plan

  variables {
    slurm_nodeset_workers = [
      merge(var.cpu_worker, { name = "instant", auto_suspend_seconds = 0 }),
      merge(var.cpu_worker, { name = "delayed", auto_suspend_seconds = 600 }),
    ]
  }

  assert {
    condition = [
      for nodeset in yamldecode(module.slurm.debug_flux_release_nodesets_rendered).nodesets.overrideValues.nodesets :
      try(nodeset.nodeConfig.autoSuspendSeconds, null)
    ] == [0, 600]
    error_message = "auto_suspend_seconds must render as nodeConfig.autoSuspendSeconds, including 0."
  }
}

run "negative_auto_suspend_seconds_is_rejected" {
  command = plan

  variables {
    slurm_nodeset_workers = [merge(var.cpu_worker, { auto_suspend_seconds = -1 })]
  }

  expect_failures = [var.slurm_nodeset_workers]
}

run "fractional_auto_suspend_seconds_is_rejected" {
  command = plan

  variables {
    slurm_nodeset_workers = [merge(var.cpu_worker, { auto_suspend_seconds = 1.5 })]
  }

  expect_failures = [var.slurm_nodeset_workers]
}
