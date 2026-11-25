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
}

run "omitted" {
  command = plan
  variables {
    slurm_nodeset_login = {
      size = 2
    }
  }
  plan_options {
    target = [module.slurm.helm_release.soperator_fluxcd_cm]
  }
  assert {
    condition     = var.slurm_nodeset_login.autoscaling == null
    error_message = "Login autoscaling must preserve defaults and explicit configuration."
  }
}

run "null" {
  command = plan
  variables {
    slurm_nodeset_login = {
      size        = 2,
      autoscaling = null
    }
  }
  plan_options {
    target = [module.slurm.helm_release.soperator_fluxcd_cm]
  }
  assert {
    condition     = var.slurm_nodeset_login.autoscaling == null
    error_message = "Login autoscaling must preserve defaults and explicit configuration."
  }
}

run "defaults" {
  command = plan
  variables {
    slurm_nodeset_login = {
      size        = 2,
      autoscaling = {}
    }
  }
  plan_options {
    target = [module.slurm.helm_release.soperator_fluxcd_cm]
  }
  assert {
    condition     = var.slurm_nodeset_login.autoscaling == { enabled = false, min_size = 1, max_size = 10, target_cpu_utilization_percentage = 70 }
    error_message = "Login autoscaling must preserve defaults and explicit configuration."
  }
}

run "explicit" {
  command = plan
  variables {
    slurm_nodeset_login = {
      size = 2,
      autoscaling = {
        enabled                           = true,
        min_size                          = 2,
        max_size                          = 3,
        target_cpu_utilization_percentage = 10
      }
    }
  }
  plan_options {
    target = [module.slurm.helm_release.soperator_fluxcd_cm]
  }
  assert {
    condition     = var.slurm_nodeset_login.autoscaling == { enabled = true, min_size = 2, max_size = 3, target_cpu_utilization_percentage = 10 }
    error_message = "Login autoscaling must preserve defaults and explicit configuration."
  }
}

run "invalid_minimum" {
  command = plan
  variables {
    slurm_nodeset_login = {
      size = 2,
      autoscaling = {
        min_size = 0
      }
    }
  }
  plan_options {
    target = [module.slurm.helm_release.soperator_fluxcd_cm]
  }
  expect_failures = [var.slurm_nodeset_login]
}

run "fractional_minimum" {
  command = plan
  variables {
    slurm_nodeset_login = {
      size = 2,
      autoscaling = {
        min_size = 1.5
      }
    }
  }
  plan_options {
    target = [module.slurm.helm_release.soperator_fluxcd_cm]
  }
  expect_failures = [var.slurm_nodeset_login]
}

run "inverted_bounds" {
  command = plan
  variables {
    slurm_nodeset_login = {
      size = 2,
      autoscaling = {
        min_size = 3,
        max_size = 2
      }
    }
  }
  plan_options {
    target = [module.slurm.helm_release.soperator_fluxcd_cm]
  }
  expect_failures = [var.slurm_nodeset_login]
}

run "fractional_maximum" {
  command = plan
  variables {
    slurm_nodeset_login = {
      size = 2,
      autoscaling = {
        max_size = 3.5
      }
    }
  }
  plan_options {
    target = [module.slurm.helm_release.soperator_fluxcd_cm]
  }
  expect_failures = [var.slurm_nodeset_login]
}

run "zero_cpu" {
  command = plan
  variables {
    slurm_nodeset_login = {
      size = 2,
      autoscaling = {
        target_cpu_utilization_percentage = 0
      }
    }
  }
  plan_options {
    target = [module.slurm.helm_release.soperator_fluxcd_cm]
  }
  expect_failures = [var.slurm_nodeset_login]
}

run "excessive_cpu" {
  command = plan
  variables {
    slurm_nodeset_login = {
      size = 2,
      autoscaling = {
        target_cpu_utilization_percentage = 101
      }
    }
  }
  plan_options {
    target = [module.slurm.helm_release.soperator_fluxcd_cm]
  }
  expect_failures = [var.slurm_nodeset_login]
}

run "fractional_cpu" {
  command = plan
  variables {
    slurm_nodeset_login = {
      size = 2,
      autoscaling = {
        target_cpu_utilization_percentage = 70.5
      }
    }
  }
  plan_options {
    target = [module.slurm.helm_release.soperator_fluxcd_cm]
  }
  expect_failures = [var.slurm_nodeset_login]
}
