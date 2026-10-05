# Mk8s cluster name.
cluster_name            = "skypilot-multiregion"
enable_secondary_region = true # Set to true to deploy in secondary region too

# SSH config
ssh_user_name = "ubuntu" # Username you want to use to connect to the nodes
ssh_public_key = {
  # key = "ssh-ed25519 AAAA... replace-with-your-public-key"
  path = "~/.ssh/id_ed25519.pub"
}

# K8s nodes
cpu_nodes_count           = 1 # Number of CPU nodes
gpu_nodes_count_per_group = 1 # Number of GPU nodes per group
gpu_node_groups           = 1 # In case you need more then 100 nodes in cluster you have to put multiple node groups
# CPU platform and presets: https://docs.nebius.com/compute/virtual-machines/types#cpu-configurations
cpu_nodes_platform = "cpu-d3"     # CPU nodes platform
cpu_nodes_preset   = "4vcpu-16gb" # CPU nodes preset
# GPU platform and preset: https://docs.nebius.com/compute/virtual-machines/types#gpu-configurations
# Specify GPU node platform and preset for each region:
gpu_nodes_platform_primary   = "gpu-h100-sxm"        # GPU nodes platform for primary region
gpu_nodes_platform_secondary = "gpu-h200-sxm"        # GPU nodes platform for secondary region (change as needed)
gpu_nodes_preset_primary     = "8gpu-128vcpu-1600gb" # GPU nodes preset for primary region
gpu_nodes_preset_secondary   = "8gpu-128vcpu-1600gb" # GPU nodes preset for secondary region (change as needed)
# Infiniband fabrics: https://docs.nebius.com/compute/clusters/gpu#fabrics
infiniband_fabric_primary   = "fabric-3" # Infiniband fabric for primary region
infiniband_fabric_secondary = "fabric-5" # Infiniband fabric for secondary region

gpu_nodes_driverfull_image = true
enable_k8s_node_group_sa   = true
enable_egress_gateway      = false
cpu_nodes_preemptible      = false
gpu_nodes_preemptible      = false

cpu_nodes_public_ips         = false
gpu_nodes_public_ips         = false
mk8s_cluster_public_endpoint = true # Set it to FALSE only in case if you've deployed the [bastion](https://github.com/nebius/nebius-solutions-library/blob/main/bastion/README.md)
# host first, and you are deploying cluster from the bastion instance

# MIG configuration
# mig_strategy =        # If set, possible values include 'single', 'mixed', 'none'
# mig_parted_config =   # If set, value will be checked against allowed for the selected 'gpu_nodes_platform'

# Observability by Nebius
enable_nebius_o11y_agent = true # Enable or disable Nebius Observability Agent deployment with true or false
enable_grafana           = true # Enable or disable Grafana® solution by Nebius with true or false
enable_mlflow_cluster    = true # Enable or disable Managed Service for MLflow in primary region

# Local Observability installation
enable_prometheus = false # Enable or disable Prometheus and Grafana deployment with true or false
enable_loki       = false # Enable or disable Loki deployment with true or false

# Storage 
enable_filestore               = false # Enable or disable Filestore integration with true or false
existing_filestore             = ""    # If enable_filestore = true, with this variable we can add existing filestore. Require string, example existing_filestore = "computefilesystem-e00r7z9vfxmg1bk99s"
filestore_disk_size_gibibytes  = 100   # Set Filestore disk size in Gbytes.
filestore_block_size_kibibytes = 4     # Set Filestore block size in bytes
