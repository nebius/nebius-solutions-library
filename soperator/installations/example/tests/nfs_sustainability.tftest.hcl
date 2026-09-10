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

run "no_nfs_is_allowed_for_xl" {
  command = plan

  variables {
    sizing_tier_override = "XL"
    nfs                  = { enabled = false }
    nfs_in_k8s           = { enabled = false }
  }

  plan_options {
    target = [terraform_data.check_nfs_sustainability]
  }
}

run "nfs_on_vds_is_prohibited_for_cluster_size_l" {
  command = plan

  variables {
    sizing_tier_override = "L"
    nfs = {
      enabled = true
      spec = {
        size_gibibytes = 93
        mount_path     = "/home"
        resource = {
          platform = "cpu-d3"
          preset   = "32vcpu-128gb"
        }
        public_ip = false
      }
    }
    nfs_in_k8s = { enabled = false }
  }

  expect_failures = [terraform_data.check_nfs_sustainability]

  plan_options {
    target = [terraform_data.check_nfs_sustainability]
  }
}

run "nfs_on_k8s_is_prohibited_for_cluster_size_l" {
  command = plan

  variables {
    sizing_tier_override = "L"
    nfs                  = { enabled = false }
    nfs_in_k8s = {
      enabled = true
      spec = {
        version         = "1.2.0"
        use_stable_repo = true
        size_gibibytes  = 93
        disk_type       = "NETWORK_SSD_IO_M3"
        filesystem_type = "ext4"
        threads         = 32
      }
    }
  }

  expect_failures = [terraform_data.check_nfs_sustainability]

  plan_options {
    target = [terraform_data.check_nfs_sustainability]
  }
}

run "nfs_on_vds_is_prohibited_for_cluster_size_xl" {
  command = plan

  variables {
    sizing_tier_override = "XL"
    nfs = {
      enabled = true
      spec = {
        size_gibibytes = 93
        mount_path     = "/home"
        resource = {
          platform = "cpu-d3"
          preset   = "32vcpu-128gb"
        }
        public_ip = false
      }
    }
    nfs_in_k8s = { enabled = false }
  }

  expect_failures = [terraform_data.check_nfs_sustainability]

  plan_options {
    target = [terraform_data.check_nfs_sustainability]
  }
}

run "nfs_on_k8s_is_prohibited_for_cluster_size_xl" {
  command = plan

  variables {
    sizing_tier_override = "XL"
    nfs                  = { enabled = false }
    nfs_in_k8s = {
      enabled = true
      spec = {
        version         = "1.2.0"
        use_stable_repo = true
        size_gibibytes  = 93
        disk_type       = "NETWORK_SSD_IO_M3"
        filesystem_type = "ext4"
        threads         = 32
      }
    }
  }

  expect_failures = [terraform_data.check_nfs_sustainability]

  plan_options {
    target = [terraform_data.check_nfs_sustainability]
  }
}
