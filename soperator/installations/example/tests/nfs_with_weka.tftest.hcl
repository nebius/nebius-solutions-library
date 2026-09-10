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

run "nfs_on_vds_with_weka_jail" {
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
    target = [
      terraform_data.check_nfs,
      terraform_data.check_nfs_exclusivity,
      terraform_data.check_nfs_sustainability,
      terraform_data.check_jail_submount_paths,
      terraform_data.check_weka_count,
    ]
  }
}

run "nfs_on_k8s_with_weka_jail" {
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
    target = [
      terraform_data.check_nfs,
      terraform_data.check_nfs_exclusivity,
      terraform_data.check_nfs_sustainability,
      terraform_data.check_jail_submount_paths,
      terraform_data.check_weka_count,
    ]
  }
}

run "nfs_on_vds_with_weka_jail_submount" {
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
  }

  plan_options {
    target = [
      terraform_data.check_nfs,
      terraform_data.check_nfs_exclusivity,
      terraform_data.check_nfs_sustainability,
      terraform_data.check_jail_submount_paths,
      terraform_data.check_weka_count,
    ]
  }
}

run "nfs_on_k8s_with_weka_jail_submount" {
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
  }

  plan_options {
    target = [
      terraform_data.check_nfs,
      terraform_data.check_nfs_exclusivity,
      terraform_data.check_nfs_sustainability,
      terraform_data.check_jail_submount_paths,
      terraform_data.check_weka_count,
    ]
  }
}
