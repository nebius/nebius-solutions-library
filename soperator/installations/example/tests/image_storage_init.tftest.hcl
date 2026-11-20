test {
  parallel = true
}

mock_provider "nebius" {}
mock_provider "flux" {}
mock_provider "units" {}
mock_provider "string-functions" {}
mock_provider "kubernetes" {}
mock_provider "helm" {}

# The image-storage disk is filled by user workloads: Docker layers and Enroot root filesystems hold
# millions of files and directory trees longer than PATH_MAX. The init container that prepares the disk
# must therefore touch a fixed set of directories and never walk Docker or Enroot data, otherwise workers
# start slowly or crash-loop on existing clusters (SCHED-3244).

variables {
  active_checks_scope              = "essential"
  company_name                     = "image-storage-test"
  slurm_login_ssh_root_public_keys = ["ssh-ed25519 test"]
  sizing_tier_override             = "S"
  nfs                              = { enabled = false }
  nfs_in_k8s                       = { enabled = false }

  slurm_nodeset_workers = [{
    name     = "cpu"
    size     = 1
    resource = { platform = "cpu-d3", preset = "16vcpu-64gb" }
    boot_disk = {
      type                 = "NETWORK_SSD"
      size_gibibytes       = 128
      block_size_kibibytes = 4
    }
    node_local_image_disk = {
      enabled = true
      spec = {
        size_gibibytes  = 930
        filesystem_type = "ext4"
        disk_type       = "NETWORK_SSD_IO_M3"
      }
    }
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

  expected_prepare_command = [
    "mkdir -p \\",
    "/volume-mount/enroot/cache \\",
    "/volume-mount/enroot/data \\",
    "/volume-mount/enroot/runtime \\",
    "/volume-mount/docker \\",
    "&& chmod 777 \\",
    "/volume-mount/enroot \\",
    "/volume-mount/enroot/cache \\",
    "/volume-mount/enroot/data \\",
    "/volume-mount/enroot/runtime \\",
    "/volume-mount/docker \\",
    "&& { chmod -R a+rw /volume-mount/enroot/cache || true; }",
  ]
}

run "image_storage_is_prepared_without_walking_user_data" {
  command = plan

  assert {
    condition = [
      for line in split("\n", module.slurm.debug_flux_release_nodesets_rendered) :
      trimspace(line) if strcontains(line, "/volume-mount/enroot") || strcontains(line, "/volume-mount/docker") || strcontains(line, "mkdir -p") || strcontains(line, "chmod 777")
    ] == var.expected_prepare_command
    error_message = <<-EOT
      The image-storage init container must run
        ${join(" ||| ", var.expected_prepare_command)}
      got
        ${join(" ||| ", [for line in split("\n", module.slurm.debug_flux_release_nodesets_rendered) : trimspace(line) if strcontains(line, "/volume-mount/enroot") || strcontains(line, "/volume-mount/docker") || strcontains(line, "mkdir -p") || strcontains(line, "chmod 777")])}
    EOT
  }

  assert {
    condition = alltrue([
      for line in split("\n", module.slurm.debug_flux_release_nodesets_rendered) :
      !strcontains(line, "-R") || strcontains(line, "chmod -R a+rw /volume-mount/enroot/cache")
      if strcontains(line, "chmod")
    ])
    error_message = "Only the flat Enroot layer cache may be changed recursively on the image-storage disk."
  }
}
