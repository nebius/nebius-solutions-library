test {
  parallel = true
}

mock_provider "nebius" {}
mock_provider "flux" {}
mock_provider "units" {}
mock_provider "string-functions" {}
mock_provider "kubernetes" {}
mock_provider "helm" {}

# The Slurm "Extra" field travels to slurmd as a *quoted* JSON document, so the
# NodeSet values must keep two levels of escaping: the YAML scalar carries the
# literal surrounding quotes and backslash-escaped inner quotes, and only after
# YAML parsing does the chart see
#   "{ \"ib_pod\": \"$TOPO_SWITCH_TIER2\", \"ib_su\": \"$TOPO_SWITCH_TIER1\" }"
# Rendering the bare { "ib_pod": ... } object instead loses one level and feeds
# slurmd an unquoted Extra.

variables {
  active_checks_scope              = "essential"
  company_name                     = "node-extra-test"
  slurm_login_ssh_root_public_keys = ["ssh-ed25519 test"]
  sizing_tier_override             = "S"
  nfs                              = { enabled = false }
  nfs_in_k8s                       = { enabled = false }

  # Written as a heredoc on purpose: HCL leaves backslashes alone here, so this
  # is byte-for-byte the line the rendered values file must contain.
  expected_dynamic_line = chomp(<<EOT
dynamic: "\"{ \\\"ib_pod\\\": \\\"$TOPO_SWITCH_TIER2\\\", \\\"ib_su\\\": \\\"$TOPO_SWITCH_TIER1\\\" }\""
EOT
  )

  cpu_worker = {
    name     = "cpu"
    size     = 1
    resource = { platform = "cpu-d3", preset = "16vcpu-64gb" }
    boot_disk = {
      type                 = "NETWORK_SSD"
      size_gibibytes       = 128
      block_size_kibibytes = 4
    }
    node_local_image_disk     = { enabled = false }
    node_local_jail_submounts = []
  }
  # A second CPU NodeSet: GPU NodeSets pull the fabric out of a mocked
  # nebius_compute_v1_gpu_cluster, which is unknown until apply and would make
  # the rendered values unreadable at plan time.
  second_cpu_worker = {
    name                      = "cpu2"
    size                      = 2
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

run "single_nodeset_renders_quoted_node_extra" {
  command = plan

  variables {
    slurm_nodeset_workers = [var.cpu_worker]
  }

  assert {
    condition = [
      for line in split("\n", module.slurm.debug_flux_release_nodesets_rendered) :
      trimspace(line) if strcontains(line, "dynamic:")
    ] == [var.expected_dynamic_line]
    error_message = <<-EOT
      NodeSet dynamic config must render as
        ${var.expected_dynamic_line}
      got
        ${join(" ||| ", [for line in split("\n", module.slurm.debug_flux_release_nodesets_rendered) : trimspace(line) if strcontains(line, "dynamic:")])}
    EOT
  }
}

# Every worker NodeSet carries the same Extra, so adding NodeSets must not
# change the escaping on any of them.
run "every_nodeset_renders_quoted_node_extra" {
  command = plan

  variables {
    slurm_nodeset_workers = [var.cpu_worker, var.second_cpu_worker]
  }

  assert {
    condition = [
      for line in split("\n", module.slurm.debug_flux_release_nodesets_rendered) :
      trimspace(line) if strcontains(line, "dynamic:")
    ] == [var.expected_dynamic_line, var.expected_dynamic_line]
    error_message = <<-EOT
      Both NodeSets must render dynamic config as
        ${var.expected_dynamic_line}
      got
        ${join(" ||| ", [for line in split("\n", module.slurm.debug_flux_release_nodesets_rendered) : trimspace(line) if strcontains(line, "dynamic:")])}
    EOT
  }
}
