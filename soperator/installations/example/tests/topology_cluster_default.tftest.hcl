test {
  parallel = true
}

mock_provider "nebius" {}
mock_provider "flux" {}
mock_provider "units" {}
mock_provider "string-functions" {}
mock_provider "kubernetes" {}
mock_provider "helm" {}

# The cluster-default topology follows the worker platforms: flat for CPU-only,
# tree-ib with GPU NodeSets, block-nvl72 with GB300 NodeSets.
# Exactly one topology is the default and the created set is unchanged by the choice.
# No mixed CPU/GB300 run: GB300 is arm64 and workers must share one CPU
# architecture, so that shape is rejected before topology selection matters.

variables {
  active_checks_scope              = "essential"
  company_name                     = "topology-test"
  slurm_login_ssh_root_public_keys = ["ssh-ed25519 test"]
  sizing_tier_override             = "S"
  nfs                              = { enabled = false }
  nfs_in_k8s                       = { enabled = false }

  cpu_worker = {
    name                  = "cpu"
    size                  = 1
    resource              = { platform = "cpu-d3", preset = "16vcpu-64gb" }
    node_local_image_disk = { enabled = false }
  }
  gpu_worker = {
    name                  = "gpu"
    size                  = 2
    resource              = { platform = "gpu-b300-sxm", preset = "8gpu-192vcpu-2768gb" }
    gpu_cluster           = { infiniband_fabric = "fabric-test" }
    node_local_image_disk = { enabled = false }
  }
  gb300_worker = {
    name                  = "gb300"
    size                  = 18
    resource              = { platform = "gpu-gb300", preset = "4gpu-112vcpu-800gb" }
    gpu_cluster           = { infiniband_fabric = "fabric-test" }
    nvlink                = { enabled = true }
    local_nvme            = { enabled = false }
    node_local_image_disk = { enabled = false }
  }

  main_partition = {
    name     = "main"
    is_all   = true
    topology = "flat"
    config   = "Default=YES PriorityTier=10 PreemptMode=OFF MaxTime=INFINITE State=UP OverSubscribe=YES"
  }
  hidden_partition = {
    name   = "hidden"
    is_all = true
    config = "Default=NO PriorityTier=10 PreemptMode=OFF Hidden=YES MaxTime=INFINITE State=UP OverSubscribe=YES"
  }
}

run "cpu_only_defaults_flat" {
  command = plan
  variables {
    slurm_nodeset_workers     = [var.cpu_worker]
    slurm_nodesets_partitions = [var.main_partition, var.hidden_partition]
  }
  plan_options {
    target = [module.slurm.helm_release.soperator_fluxcd_cm]
  }
  assert {
    condition     = local.slurm_named_topologies[*].name == ["flat"] && [for t in local.slurm_named_topologies : t.name if t.cluster_default] == ["flat"]
    error_message = "CPU-only: expected only flat, with flat as the cluster default."
  }
}

run "gpu_defaults_tree_ib" {
  command = plan
  variables {
    slurm_nodeset_workers     = [var.gpu_worker]
    slurm_nodesets_partitions = [var.main_partition, var.hidden_partition]
  }
  plan_options {
    target = [module.slurm.helm_release.soperator_fluxcd_cm]
  }
  assert {
    condition     = local.slurm_named_topologies[*].name == ["flat", "tree-ib"] && [for t in local.slurm_named_topologies : t.name if t.cluster_default] == ["tree-ib"]
    error_message = "GPU without GB300: expected flat and tree-ib, with tree-ib as the cluster default."
  }
}

run "gb300_defaults_block_nvl72" {
  command = plan
  variables {
    region                    = "eu-north1"
    slurm_nodeset_workers     = [var.gb300_worker]
    slurm_nodesets_partitions = [var.main_partition, var.hidden_partition]
  }
  plan_options {
    target = [module.slurm.helm_release.soperator_fluxcd_cm]
  }
  assert {
    condition     = local.login_on_worker_nodes && !local.login_node_group.node_group_enabled && local.populate_jail_node_filter_name == "worker"
    error_message = "GB300 without regional ARM login availability must keep login pods and populate-jail on workers."
  }
  assert {
    condition     = local.slurm_named_topologies[*].name == ["flat", "tree-ib", "block-nvl72"] && [for t in local.slurm_named_topologies : t.name if t.cluster_default] == ["block-nvl72"]
    error_message = "GB300: expected flat, tree-ib and block-nvl72, with block-nvl72 as the cluster default."
  }
  assert {
    condition = (
      length(local.slurm_nodeset_worker_ranges) == 1 &&
      local.slurm_nodeset_worker_ranges[0].name == "gb300" &&
      local.slurm_nodeset_worker_ranges[0].first_ordinal == 0 &&
      local.slurm_nodeset_worker_ranges[0].last_ordinal == 0
    )
    error_message = "GB300: expected one complete rack to be represented by Helm range 0..0."
  }
}

run "gb300_complete_racks_share_one_range" {
  command = plan
  variables {
    region                    = "eu-north1"
    slurm_nodeset_workers     = [merge(var.gb300_worker, { size = 36 })]
    slurm_nodesets_partitions = [var.main_partition, var.hidden_partition]
  }
  plan_options {
    target = [module.slurm.helm_release.soperator_fluxcd_cm]
  }
  assert {
    condition = (
      local.slurm_nodeset_workers[*].name == ["gb300-rack0", "gb300-rack1"] &&
      local.slurm_nodeset_workers[*].size == [18, 18]
    )
    error_message = "GB300 complete racks: expected two expanded 18-node racks."
  }
  assert {
    condition = (
      length(local.slurm_nodeset_worker_ranges) == 1 &&
      local.slurm_nodeset_worker_ranges[0].first_ordinal == 0 &&
      local.slurm_nodeset_worker_ranges[0].last_ordinal == 1
    )
    error_message = "GB300 complete racks: expected one Helm range covering ordinals 0..1."
  }
}

run "gb300_single_partial_rack_does_not_use_range" {
  command = plan
  variables {
    region                    = "eu-north1"
    slurm_nodeset_workers     = [merge(var.gb300_worker, { size = 10 })]
    slurm_nodesets_partitions = [var.main_partition, var.hidden_partition]
  }
  plan_options {
    target = [module.slurm.helm_release.soperator_fluxcd_cm]
  }
  assert {
    condition = (
      local.slurm_nodeset_workers[*].name == ["gb300-rack0"] &&
      local.slurm_nodeset_workers[*].size == [10] &&
      length(local.slurm_nodeset_worker_ranges) == 0
    )
    error_message = "GB300 partial rack: expected one standalone 10-node NodeSet without a Helm range."
  }
}

run "mixed_cpu_gpu_defaults_tree_ib" {
  command = plan
  variables {
    slurm_nodeset_workers     = [var.cpu_worker, var.gpu_worker]
    slurm_nodesets_partitions = [var.main_partition, var.hidden_partition]
  }
  plan_options {
    target = [module.slurm.helm_release.soperator_fluxcd_cm]
  }
  assert {
    condition     = local.slurm_named_topologies[*].name == ["flat", "tree-ib"] && [for t in local.slurm_named_topologies : t.name if t.cluster_default] == ["tree-ib"]
    error_message = "Mixed CPU/GPU: expected flat and tree-ib, with tree-ib as the cluster default."
  }
}

# A partition spanning CPU NodeSets cannot be bound to the GPU cluster default;
# it must stay explicitly bound to flat.
run "mixed_cpu_gpu_rejects_tree_ib_on_all_nodesets" {
  command = plan
  variables {
    slurm_nodeset_workers     = [var.cpu_worker, var.gpu_worker]
    slurm_nodesets_partitions = [merge(var.main_partition, { topology = "tree-ib" }), var.hidden_partition]
  }
  plan_options {
    target = [module.slurm.helm_release.soperator_fluxcd_cm]
  }
  expect_failures = [var.slurm_nodesets_partitions]
}

run "automatic_colocation_rejects_zero_cpu_reserve" {
  command = plan
  variables {
    region                      = "eu-north1"
    slurm_nodeset_workers       = [var.gb300_worker]
    slurm_nodesets_partitions   = [var.main_partition, var.hidden_partition]
    slurm_login_on_worker_nodes = null
    gb300_login_pod_worker_reserve = {
      cpu_cores                   = 0
      memory_gibibytes            = 32
      ephemeral_storage_gibibytes = 128
    }
  }
  plan_options {
    target = [terraform_data.check_slurm_worker_cpu_platform]
  }
  expect_failures = [var.gb300_login_pod_worker_reserve]
}

run "automatic_colocation_rejects_negative_memory_reserve" {
  command = plan
  variables {
    region                      = "eu-north1"
    slurm_nodeset_workers       = [var.gb300_worker]
    slurm_nodesets_partitions   = [var.main_partition, var.hidden_partition]
    slurm_login_on_worker_nodes = null
    gb300_login_pod_worker_reserve = {
      cpu_cores                   = 8
      memory_gibibytes            = -1
      ephemeral_storage_gibibytes = 128
    }
  }
  plan_options {
    target = [terraform_data.check_slurm_worker_cpu_platform]
  }
  expect_failures = [var.gb300_login_pod_worker_reserve]
}

run "automatic_colocation_rejects_zero_storage_reserve" {
  command = plan
  variables {
    region                      = "eu-north1"
    slurm_nodeset_workers       = [var.gb300_worker]
    slurm_nodesets_partitions   = [var.main_partition, var.hidden_partition]
    slurm_login_on_worker_nodes = null
    gb300_login_pod_worker_reserve = {
      cpu_cores                   = 8
      memory_gibibytes            = 32
      ephemeral_storage_gibibytes = 0
    }
  }
  plan_options {
    target = [terraform_data.check_slurm_worker_cpu_platform]
  }
  expect_failures = [var.gb300_login_pod_worker_reserve]
}
