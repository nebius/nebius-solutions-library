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
  slurm_nodeset_system = {
    min_size = 3
    max_size = 9
    resource = {
      platform = "cpu-d3"
      preset   = "32vcpu-128gb"
    }
    boot_disk = {
      type                 = "NETWORK_SSD"
      size_gibibytes       = 128
      block_size_kibibytes = 4
    }
  }

  slurm_nodeset_controller = {
    size = 1
    resource = {
      platform = "cpu-d3"
      preset   = "32vcpu-128gb"
    }
    boot_disk = {
      type                 = "NETWORK_SSD"
      size_gibibytes       = 128
      block_size_kibibytes = 4
    }
  }

  slurm_nodeset_workers = [{
    name = "worker"
    size = 1
    resource = {
      platform = "cpu-d3"
      preset   = "32vcpu-128gb"
    }
    boot_disk = {
      type                 = "NETWORK_SSD"
      size_gibibytes       = 128
      block_size_kibibytes = 4
    }
    node_local_image_disk     = { enabled = false }
    node_local_jail_submounts = []
  }]

  slurm_nodeset_login = {
    size = 1
    resource = {
      platform = "cpu-d3"
      preset   = "32vcpu-128gb"
    }
    boot_disk = {
      type                 = "NETWORK_SSD"
      size_gibibytes       = 256
      block_size_kibibytes = 4
    }
  }

  accounting_enabled = true
  slurm_nodeset_accounting = {
    resource = {
      platform = "cpu-d3"
      preset   = "32vcpu-128gb"
    }
    boot_disk = {
      type                 = "NETWORK_SSD"
      size_gibibytes       = 128
      block_size_kibibytes = 4
    }
  }

  filesystem_jail = {
    spec = {
      type                 = "NETWORK_SSD"
      size_gibibytes       = 2048
      block_size_kibibytes = 4
    }
  }

  filesystem_jail_submounts = [{
    name       = "data"
    mount_path = "/data"
    spec = {
      type                 = "WEKA"
      size_gibibytes       = 2048
      block_size_kibibytes = 4
    }
  }]
}

run "one_weka_jail_submount_is_allowed" {
  command = plan

  plan_options {
    target = [terraform_data.check_weka_count]
  }
}

run "one_weka_jail_is_allowed" {
  command = plan

  variables {
    filesystem_jail = {
      spec = {
        type                 = "WEKA"
        size_gibibytes       = 2048
        block_size_kibibytes = 4
      }
    }
    filesystem_jail_submounts = [{
      name       = "data"
      mount_path = "/data"
      spec = {
        type                 = "NETWORK_SSD"
        size_gibibytes       = 2048
        block_size_kibibytes = 4
      }
    }]
  }

  plan_options {
    target = [terraform_data.check_weka_count]
  }
}

run "two_weka_filesystems_are_prohibited" {
  command = plan

  variables {
    filesystem_jail = {
      spec = {
        type                 = "WEKA"
        size_gibibytes       = 2048
        block_size_kibibytes = 4
      }
    }
  }

  expect_failures = [terraform_data.check_weka_count]

  plan_options {
    target = [terraform_data.check_weka_count]
  }
}

run "weka_is_allowed_with_32vcpu_cpu_presets" {
  command = plan

  plan_options {
    target = [terraform_data.check_resource_presets_for_weka]
  }
}

run "weka_is_prohibited_with_16vcpu_system_preset" {
  command = plan

  variables {
    slurm_nodeset_system = {
      min_size = 3
      max_size = 9
      resource = {
        platform = "cpu-d3"
        preset   = "16vcpu-64gb"
      }
      boot_disk = {
        type                 = "NETWORK_SSD"
        size_gibibytes       = 128
        block_size_kibibytes = 4
      }
    }
  }

  expect_failures = [terraform_data.check_resource_presets_for_weka]

  plan_options {
    target = [terraform_data.check_resource_presets_for_weka]
  }
}

run "weka_is_prohibited_with_16vcpu_controller_preset" {
  command = plan

  variables {
    slurm_nodeset_controller = {
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
    }
  }

  expect_failures = [terraform_data.check_resource_presets_for_weka]

  plan_options {
    target = [terraform_data.check_resource_presets_for_weka]
  }
}

run "weka_is_prohibited_with_16vcpu_worker_preset" {
  command = plan

  variables {
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
  }

  expect_failures = [terraform_data.check_resource_presets_for_weka]

  plan_options {
    target = [terraform_data.check_resource_presets_for_weka]
  }
}

run "weka_is_prohibited_with_16vcpu_login_preset" {
  command = plan

  variables {
    slurm_nodeset_login = {
      size = 1
      resource = {
        platform = "cpu-d3"
        preset   = "16vcpu-64gb"
      }
      boot_disk = {
        type                 = "NETWORK_SSD"
        size_gibibytes       = 256
        block_size_kibibytes = 4
      }
    }
  }

  expect_failures = [terraform_data.check_resource_presets_for_weka]

  plan_options {
    target = [terraform_data.check_resource_presets_for_weka]
  }
}

run "weka_is_prohibited_with_16vcpu_accounting_preset" {
  command = plan

  variables {
    accounting_enabled = true
    slurm_nodeset_accounting = {
      resource = {
        platform = "cpu-d3"
        preset   = "16vcpu-64gb"
      }
      boot_disk = {
        type                 = "NETWORK_SSD"
        size_gibibytes       = 128
        block_size_kibibytes = 4
      }
    }
  }

  expect_failures = [terraform_data.check_resource_presets_for_weka]

  plan_options {
    target = [terraform_data.check_resource_presets_for_weka]
  }
}
