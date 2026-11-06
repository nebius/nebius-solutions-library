# The registry bootstrap at boot (docs/IMAGES.md "The setting at boot"): a CPU batch pool gets the same fragment a GPU
# pool gets (mirror file for the logical host on the loopback Zot NodePort, containerd drop-in, unlimited memlock, the
# node-config marker, one containerd restart at boot), the system node group keeps its plain cloud-init, and without
# the image cache no registry configuration is written anywhere. Mocked provider: nothing is created.
mock_provider "nebius" {}

variables {
  name               = "bootstrap-test"
  project_id         = "project-example"
  subnet_id          = "vpcsubnet-example"
  kubernetes_version = "1.35"
  protect_data       = false
  public_ip          = false
  system_pool        = { platform = "cpu-d3", preset = "4vcpu-16gb", node_count = 2 }
  image_cache        = { host = "registry.serverless2.local", node_port = 30500 }
  weights_filesystem = { enabled = true, size_gib = 1024, type = "NETWORK_SSD" }
  cpu_pools = {
    batch   = { platform = "cpu-d3", preset = "32vcpu-128gb", min_nodes = 0, max_nodes = 2, boot_disk_gib = 128, labels = {} }
    noshare = { platform = "cpu-d3", preset = "4vcpu-16gb", min_nodes = 0, max_nodes = 1, boot_disk_gib = 64, labels = {}, shared_filesystem = false }
  }
  gpu_pools = {
    gpu = {
      platform      = "gpu-h100-sxm", preset = "8gpu-128vcpu-1600gb", gpu_class = "h100"
      capacity      = { type = "on_demand", reservation_ids = [] }
      min_nodes     = 0, max_nodes = 1, endpoint_floor_gpus = 0
      driver_preset = "cuda13.0", boot_disk_gib = 512
      local_nvme    = false, local_nvme_mode = "kubelet-ephemeral"
      interconnect  = "none", labels = {}
    }
  }
}

run "cpu_batch_first_pull_goes_through_the_mirror_files_written_at_boot" {
  command = plan
  assert {
    condition = alltrue([
      strcontains(nebius_mk8s_v1_node_group.cpu["batch"].template.cloud_init_user_data, "path: /etc/containerd/certs.d/registry.serverless2.local/hosts.toml"),
      strcontains(nebius_mk8s_v1_node_group.cpu["batch"].template.cloud_init_user_data, "server = \"http://127.0.0.1:30500\""),
      strcontains(nebius_mk8s_v1_node_group.cpu["batch"].template.cloud_init_user_data, "path: /etc/containerd/conf.d/serverless2-registry.toml"),
      strcontains(nebius_mk8s_v1_node_group.cpu["batch"].template.cloud_init_user_data, "config_path = \"/etc/containerd/certs.d\""),
    ])
    error_message = "A CPU batch pool must get the mirror file for the logical host and the containerd drop-in at boot."
  }
  assert {
    condition = alltrue([
      strcontains(nebius_mk8s_v1_node_group.cpu["batch"].template.cloud_init_user_data, "LimitMEMLOCK=infinity"),
      strcontains(nebius_mk8s_v1_node_group.cpu["batch"].template.cloud_init_user_data, "touch /etc/containerd/certs.d/.serverless2-containerd-restarted"),
      strcontains(nebius_mk8s_v1_node_group.cpu["batch"].template.cloud_init_user_data, "systemctl restart containerd"),
    ])
    error_message = "A CPU batch pool must lift memlock, write the node-config marker and restart containerd once at boot (never post-Ready)."
  }
  assert {
    condition = alltrue([
      strcontains(nebius_mk8s_v1_node_group.cpu["batch"].template.cloud_init_user_data, "fs.inotify.max_user_instances = 8192"),
      strcontains(nebius_mk8s_v1_node_group.cpu["batch"].template.cloud_init_user_data, "mount -t virtiofs weights /mnt/weights"),
      !strcontains(nebius_mk8s_v1_node_group.cpu["noshare"].template.cloud_init_user_data, "mount -t virtiofs weights"),
      strcontains(nebius_mk8s_v1_node_group.cpu["noshare"].template.cloud_init_user_data, "hosts.toml"),
    ])
    error_message = "The existing CPU pool configuration is preserved: the sysctl, the shared-filesystem mount where the pool has it (and nowhere else), the bootstrap on every batch pool."
  }
  assert {
    condition     = length(regexall("write_files:", nebius_mk8s_v1_node_group.cpu["batch"].template.cloud_init_user_data)) == 1 && length(regexall("runcmd:", nebius_mk8s_v1_node_group.cpu["batch"].template.cloud_init_user_data)) == 1
    error_message = "One write_files and one runcmd section: a valid cloud-config."
  }
  assert {
    condition     = nebius_mk8s_v1_node_group.system.template.cloud_init_user_data == local.system_cloud_init && !strcontains(nebius_mk8s_v1_node_group.system.template.cloud_init_user_data, "hosts.toml")
    error_message = "The system node group keeps its plain cloud-init: no registry bootstrap there (node-config serves it)."
  }
  assert {
    condition = alltrue([
      strcontains(nebius_mk8s_v1_node_group.gpu["gpu"].template.cloud_init_user_data, "path: /etc/containerd/certs.d/registry.serverless2.local/hosts.toml"),
      strcontains(nebius_mk8s_v1_node_group.gpu["gpu"].template.cloud_init_user_data, "LimitMEMLOCK=infinity"),
      strcontains(nebius_mk8s_v1_node_group.gpu["gpu"].template.cloud_init_user_data, "touch /etc/containerd/certs.d/.serverless2-containerd-restarted"),
      strcontains(nebius_mk8s_v1_node_group.gpu["gpu"].template.cloud_init_user_data, "fs.inotify.max_user_instances = 8192"),
    ])
    error_message = "The GPU pools keep the same bootstrap (the shared fragment)."
  }
}

run "without_the_cache_no_registry_configuration_is_written" {
  command = plan
  variables {
    image_cache = { enabled = false, host = "registry.serverless2.local", node_port = 30500 }
  }
  assert {
    condition = alltrue([
      !strcontains(nebius_mk8s_v1_node_group.cpu["batch"].template.cloud_init_user_data, "hosts.toml"),
      !strcontains(nebius_mk8s_v1_node_group.cpu["batch"].template.cloud_init_user_data, "serverless2-registry.toml"),
      !strcontains(nebius_mk8s_v1_node_group.cpu["batch"].template.cloud_init_user_data, "containerd-restarted"),
      !strcontains(nebius_mk8s_v1_node_group.gpu["gpu"].template.cloud_init_user_data, "hosts.toml"),
      strcontains(nebius_mk8s_v1_node_group.cpu["batch"].template.cloud_init_user_data, "mount -t virtiofs weights /mnt/weights"),
    ])
    error_message = "Without the image cache the node image's registry configuration is kept on every pool; the CPU pool's existing cloud-init stands."
  }
}
