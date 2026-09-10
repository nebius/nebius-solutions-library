test {
  parallel = true
}

mock_provider "nebius" {}
mock_provider "flux" {}
mock_provider "units" {}
mock_provider "string-functions" {}
mock_provider "kubernetes" {}
mock_provider "helm" {}

run "home_submount_is_allowed_without_nfs" {
  command = plan

  variables {
    nfs        = { enabled = false }
    nfs_in_k8s = { enabled = false }
    filesystem_jail_submounts = [{
      name       = "home"
      mount_path = "/home"
      spec = {
        type                 = "NETWORK_SSD"
        size_gibibytes       = 2048
        block_size_kibibytes = 4
      }
    }]
  }

  plan_options {
    target = [terraform_data.check_jail_submount_paths]
  }
}

run "home_submount_is_prohibited_with_home_at_nfs_on_vds" {
  command = plan

  variables {
    nfs = {
      enabled = true
      spec = {
        size_gibibytes = 40 * 93
        mount_path     = "/home"
        resource = {
          platform = "cpu-d3"
          preset   = "32vcpu-128gb"
        }
        public_ip = false
      }
    }
    nfs_in_k8s = { enabled = false }
    filesystem_jail_submounts = [{
      name       = "home"
      mount_path = "/home"
      spec = {
        type                 = "NETWORK_SSD"
        size_gibibytes       = 2048
        block_size_kibibytes = 4
      }
    }]
  }

  expect_failures = [
    terraform_data.check_jail_submount_paths,
  ]

  plan_options {
    target = [terraform_data.check_jail_submount_paths]
  }
}

run "home_submount_is_allowed_with_other_mount_of_nfs_on_vds" {
  command = plan

  variables {
    nfs = {
      enabled = true
      spec = {
        size_gibibytes = 40 * 93
        mount_path     = "/test"
        resource = {
          platform = "cpu-d3"
          preset   = "32vcpu-128gb"
        }
        public_ip = false
      }
    }
    nfs_in_k8s = { enabled = false }
    filesystem_jail_submounts = [{
      name       = "home"
      mount_path = "/home"
      spec = {
        type                 = "NETWORK_SSD"
        size_gibibytes       = 2048
        block_size_kibibytes = 4
      }
    }]
  }

  plan_options {
    target = [terraform_data.check_jail_submount_paths]
  }
}

run "home_submount_is_prohibited_with_nfs_on_k8s" {
  command = plan

  variables {
    nfs = { enabled = false }
    nfs_in_k8s = {
      enabled = true
      spec = {
        version         = "1.2.0"
        use_stable_repo = true
        size_gibibytes  = 40 * 93
        disk_type       = "NETWORK_SSD_IO_M3"
        filesystem_type = "ext4"
        threads         = 128
      }
    }
    filesystem_jail_submounts = [{
      name       = "home"
      mount_path = "/home"
      spec = {
        type                 = "NETWORK_SSD"
        size_gibibytes       = 2048
        block_size_kibibytes = 4
      }
    }]
  }

  expect_failures = [
    terraform_data.check_jail_submount_paths,
  ]

  plan_options {
    target = [terraform_data.check_jail_submount_paths]
  }
}

run "exclusive_paths_are_allowed" {
  command = plan

  variables {
    nfs        = { enabled = false }
    nfs_in_k8s = { enabled = false }
    filesystem_jail_submounts = [{
      name       = "home"
      mount_path = "/home"
      spec = {
        type                 = "NETWORK_SSD"
        size_gibibytes       = 2048
        block_size_kibibytes = 4
      }
      }, {
      name       = "dome"
      mount_path = "/dome"
      spec = {
        type                 = "NETWORK_SSD"
        size_gibibytes       = 2048
        block_size_kibibytes = 4
      }
    }]
  }

  plan_options {
    target = [terraform_data.check_jail_submount_paths]
  }
}

run "duplicate_paths_are_prohibited" {
  command = plan

  variables {
    nfs        = { enabled = false }
    nfs_in_k8s = { enabled = false }
    filesystem_jail_submounts = [{
      name       = "home"
      mount_path = "/home"
      spec = {
        type                 = "NETWORK_SSD"
        size_gibibytes       = 2048
        block_size_kibibytes = 4
      }
      }, {
      name       = "dome"
      mount_path = "/home"
      spec = {
        type                 = "NETWORK_SSD"
        size_gibibytes       = 2048
        block_size_kibibytes = 4
      }
    }]
  }

  expect_failures = [
    terraform_data.check_jail_submount_paths,
  ]

  plan_options {
    target = [terraform_data.check_jail_submount_paths]
  }
}

run "no_submounts_is_fine" {
  command = plan

  variables {
    nfs                        = { enabled = false }
    nfs_in_k8s                 = { enabled = false }
    allow_empty_jail_submounts = true
    filesystem_jail_submounts  = []
  }

  plan_options {
    target = [terraform_data.check_jail_submount_paths]
  }
}
