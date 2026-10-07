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
  active_checks_scope              = "testing"
  company_name                     = "storage-test"
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
  sizing_tier_override = "S"
  nfs                  = { enabled = false }
}

run "new_cluster_does_not_create_legacy_filestores" {
  command = plan

  variables {
    accounting_enabled = true
  }

  plan_options {
    target = [
      module.filesystem.nebius_compute_v1_filesystem.controller_spool,
      module.filesystem.nebius_compute_v1_filesystem.accounting,
      module.slurm.helm_release.soperator_fluxcd_cm,
    ]
  }

  assert {
    condition     = module.filesystem.controller_spool == null
    error_message = "A new cluster must not create the legacy controller-spool Filestore."
  }

  assert {
    condition     = module.filesystem.accounting == null
    error_message = "A new cluster must not create the legacy accounting Filestore."
  }
}

run "legacy_filestores_remain_configured" {
  command = plan

  variables {
    accounting_enabled = true
    filestore_controller_spool = {
      spec = {
        size_gibibytes       = 128
        block_size_kibibytes = 4
      }
    }
    filestore_accounting = {
      spec = {
        size_gibibytes       = 512
        block_size_kibibytes = 4
      }
    }
  }

  plan_options {
    target = [
      module.filesystem.nebius_compute_v1_filesystem.controller_spool,
      module.filesystem.nebius_compute_v1_filesystem.accounting,
      module.slurm.helm_release.soperator_fluxcd_cm,
    ]
  }

  assert {
    condition     = module.filesystem.controller_spool != null
    error_message = "An explicitly configured controller-spool Filestore must be retained."
  }

  assert {
    condition     = module.filesystem.accounting != null
    error_message = "An explicitly configured accounting Filestore must be retained."
  }
}
