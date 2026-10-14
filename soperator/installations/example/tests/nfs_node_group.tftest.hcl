test {
  parallel = true
}

mock_provider "nebius" {}
mock_provider "flux" {}
mock_provider "units" {}
mock_provider "string-functions" {}
mock_provider "kubernetes" {}
mock_provider "helm" {}

variables {
  active_checks_scope              = "essential"
  company_name                     = "nfs-test"
  slurm_login_ssh_root_public_keys = ["ssh-ed25519 test"]
  slurm_nodeset_workers = [{
    name = "worker"
    size = 1
    resource = {
      platform = "cpu-d3"
      preset   = "16vcpu-64gb"
    }
    boot_disk = {
      type                 = "NETWORK_SSD"
      size_gibibytes       = 128
      block_size_kibibytes = 4
    }
    node_local_image_disk     = { enabled = false }
    node_local_jail_submounts = []
  }]
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
  sizing_tier_override = "S"
  nfs                  = { enabled = false }
  nfs_in_k8s = {
    enabled = true
    spec = {
      version         = "1.2.0"
      use_stable_repo = true
      size_gibibytes  = 3720
      disk_type       = "NETWORK_SSD_IO_M3"
      filesystem_type = "ext4"
      threads         = 128
      node_group = {
        resource = {
          platform = "cpu-d3"
        }
        boot_disk = {
          type                 = "NETWORK_SSD"
          size_gibibytes       = 128
          block_size_kibibytes = 4
        }
      }
    }
  }
}

run "disabled_without_spec" {
  command = plan

  variables {
    nfs_in_k8s = { enabled = false }
  }

  plan_options {
    target = [
      terraform_data.check_slurm_nodeset,
      module.k8s.nebius_mk8s_v1_node_group.nfs,
      module.slurm.helm_release.soperator_fluxcd_cm,
    ]
  }

  assert {
    condition     = local.slurm_nodeset_nfs == null
    error_message = "Disabled NFS must not configure a node group."
  }
}

run "disabled_with_retained_spec" {
  command = plan

  variables {
    nfs_in_k8s = merge(var.nfs_in_k8s, { enabled = false })
  }

  plan_options {
    target = [
      terraform_data.check_slurm_nodeset,
      module.k8s.nebius_mk8s_v1_node_group.nfs,
      module.slurm.helm_release.soperator_fluxcd_cm,
    ]
  }

  assert {
    condition     = local.slurm_nodeset_nfs == null
    error_message = "Retaining the NFS spec must not keep its node group enabled."
  }
}

run "enabled_with_node_group" {
  command = plan

  plan_options {
    target = [
      terraform_data.check_slurm_nodeset,
      module.k8s.nebius_mk8s_v1_node_group.nfs,
      module.slurm.helm_release.soperator_fluxcd_cm,
    ]
  }

  assert {
    condition     = local.slurm_nodeset_nfs.size == 1 && local.slurm_nodeset_nfs.resource.preset == module.sizing.node_preset.nfs
    error_message = "Enabled NFS must configure one node using the sizing-tier preset when none is specified."
  }
}

run "enabled_requires_node_group" {
  command = plan

  variables {
    nfs_in_k8s = merge(var.nfs_in_k8s, {
      spec = merge(var.nfs_in_k8s.spec, { node_group = null })
    })
  }

  plan_options {
    target = [
      terraform_data.check_slurm_nodeset,
      module.k8s.nebius_mk8s_v1_node_group.nfs,
      module.slurm.helm_release.soperator_fluxcd_cm,
    ]
  }

  expect_failures = [var.nfs_in_k8s]
}
