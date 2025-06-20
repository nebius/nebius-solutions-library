# SSH config
ssh_user_name = "ubuntu" # Username you want to use to connect to the nodes
ssh_public_key = {
  key = "put customers public ssh key here"
  # path = "put path to public ssh key here"
}

# K8s nodes
cpu_nodes_count            = 2                     # Number of CPU nodes
gpu_nodes_count_per_group  = 2                     # Number of GPU nodes per group
gpu_node_groups            = 1                     # In case you need more then 100 nodes in cluster you have to put multiple node groups
cpu_nodes_platform         = "cpu-d3"              # CPU nodes platform
cpu_nodes_preset           = "4vcpu-16gb"          # CPU nodes preset
gpu_nodes_platform         = "gpu-h200-sxm"        # GPU nodes platform
gpu_nodes_preset           = "8gpu-128vcpu-1600gb" # GPU nodes preset
infiniband_fabric          = ""                    # Infiniband fabric name.
gpu_nodes_driverfull_image = true
enable_k8s_node_group_sa   = true
enable_egress_gateway      = false
egress_nodes_preset        = "4vcpu-16gb"

# MIG configuration
# mig_strategy =        # If set, possible values include 'single', 'mixed', 'none'
# mig_parted_config =   # If set, value will be checked against allowed for the selected 'gpu_nodes_platform'

# Observability
enable_prometheus = true  # Enable or disable Prometheus and Grafana deployment with true or false
enable_loki       = false # Enable or disable Loki deployment with true or false

## Loki
# loki_access_key_id = "" # See the instruction in README.md on how to create this. Leave empty if you are not deploying Loki.
# loki_secret_key    = "" # See the instruction in README.md on how to create this. Leave empty if you are not deploying Loki.

# Storage
## Filestore - recommended
enable_filestore     = true                             # Enable or disable Filestore integration with true or false
filestore_disk_size  = 10 * (1024 * 1024 * 1024 * 1024) # Set Filestore disk size in bytes. The multiplication makes it easier to set the size in TB. This would set the size as 10TB
filestore_block_size = 4096                             # Set Filestore block size in bytes

# KubeRay
enable_kuberay           = false # Turn KubeRay to false, otherwise gpu capacity will be consumed by KubeRay cluster
kuberay_min_gpu_replicas = 1
kuberay_max_gpu_replicas = 2
