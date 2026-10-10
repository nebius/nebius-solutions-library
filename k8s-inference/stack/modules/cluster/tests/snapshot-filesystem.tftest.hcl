# Mocked provider: these tests never create cloud resources.
mock_provider "nebius" {}

variables {
  name               = "snapshot-test"
  project_id         = "project-example"
  subnet_id          = "vpcsubnet-example"
  kubernetes_version = "1.35"
  protect_data       = false
  public_ip          = false
  system_pool        = { platform = "cpu-d3", preset = "4vcpu-16gb", node_count = 2 }
  image_cache        = { host = "images.example.invalid", node_port = 30500 }
  weights_filesystem = { enabled = true, size_gib = 1024, type = "NETWORK_SSD" }
  cpu_pools = {
    batch = { platform = "cpu-d3", preset = "4vcpu-16gb", min_nodes = 0, max_nodes = 1, boot_disk_gib = 128, labels = {} }
  }
  gpu_pools = {
    gpu = {
      platform      = "gpu-h100-sxm", preset = "1gpu-16vcpu-200gb", gpu_class = "h100"
      capacity      = { type = "on-demand", reservation_ids = [] }
      min_nodes     = 0, max_nodes = 1, endpoint_floor_gpus = 0
      driver_preset = "cuda13.0", boot_disk_gib = 512
      local_nvme    = false, local_nvme_mode = "kubelet-ephemeral"
      interconnect  = "none", labels = {}
    }
  }
}

run "default_preserves_system_and_batch_nodes" {
  command = plan
  assert {
    condition     = nebius_mk8s_v1_node_group.system.template.filesystems == null
    error_message = "The default must not attach a filesystem to system nodes."
  }
  assert {
    condition     = nebius_mk8s_v1_node_group.system.template.cloud_init_user_data == local.system_cloud_init
    error_message = "The default must preserve existing system cloud-init."
  }
  assert {
    condition     = nebius_mk8s_v1_node_group.cpu["batch"].template.cloud_init_user_data == local.system_cloud_init
    error_message = "Batch CPU nodes must retain their original cloud-init."
  }
  assert {
    condition     = !contains(keys(nebius_mk8s_v1_node_group.gpu["gpu"].template.metadata.labels), "serverless2.nebius/snapshot-store")
    error_message = "Default GPU pools must not claim snapshot-store qualification."
  }
}

run "shared_store_survives_gpu_scale_to_zero" {
  command = plan
  variables {
    weights_filesystem = { enabled = true, size_gib = 1024, type = "NETWORK_SSD", mount_on_system = true }
  }
  assert {
    condition     = length(nebius_mk8s_v1_node_group.system.template.filesystems) == 1
    error_message = "The CPU operator's system nodes need the shared filesystem attached."
  }
  assert {
    condition     = nebius_mk8s_v1_node_group.system.template.metadata.labels["serverless2.nebius/snapshot-store"] == "true"
    error_message = "CPU operator nodes must be selectable as shared-store nodes."
  }
  assert {
    condition     = !contains(keys(nebius_mk8s_v1_node_group.gpu["gpu"].template.metadata.labels), "serverless2.nebius/snapshot-store")
    error_message = "CPU storage attachment must not relabel or roll an existing warm GPU pool. Qualify GPU pools explicitly."
  }
  assert {
    condition     = !strcontains(nebius_mk8s_v1_node_group.gpu["gpu"].template.cloud_init_user_data, "gpu-snapshot")
    error_message = "CPU storage attachment must not change GPU cloud-init. The shared directory is created from CPU nodes."
  }
  assert {
    condition     = strcontains(nebius_mk8s_v1_node_group.system.template.cloud_init_user_data, "mountpoint -q /mnt/weights && mkdir -p /mnt/weights/gpu-snapshot")
    error_message = "A failed mount must not create snapshots on a node's boot disk."
  }
  assert {
    condition     = nebius_mk8s_v1_node_group.cpu["batch"].template.cloud_init_user_data == local.system_cloud_init
    error_message = "Opt-in system mounting must not modify unrelated batch CPU nodes."
  }
}
