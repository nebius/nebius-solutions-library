# =============================================================================
# OSMO on Nebius - Root Module
# =============================================================================

# -----------------------------------------------------------------------------
# Platform Module (VPC, Storage, PostgreSQL, Container Registry)
# -----------------------------------------------------------------------------
module "platform" {
  source = "./modules/platform"

  parent_id   = var.parent_id
  tenant_id   = var.tenant_id
  region      = var.region
  name_prefix = local.name_prefix

  # Network (existing default network and subnet)
  network_id = var.network_id
  subnet_id  = var.subnet_id

  # Storage
  storage_bucket_name = local.storage_bucket_name

  # Filestore
  enable_filestore         = var.enable_filestore
  filestore_disk_type      = var.filestore_disk_type
  filestore_size_gib       = var.filestore_size_gib
  filestore_block_size_kib = var.filestore_block_size_kib

  # PostgreSQL (optional - can use in-cluster PostgreSQL instead)
  enable_managed_postgresql = var.enable_managed_postgresql
  postgresql_version        = var.postgresql_version
  postgresql_public_access  = var.postgresql_public_access
  postgresql_platform       = local.postgresql_platform
  postgresql_preset         = var.postgresql_preset
  postgresql_disk_type      = local.postgresql_disk_type
  postgresql_disk_size_gib  = var.postgresql_disk_size_gib
  postgresql_host_count     = var.postgresql_host_count
  postgresql_database_name  = var.postgresql_database_name
  postgresql_username       = var.postgresql_username

  # Container Registry
  enable_container_registry = var.enable_container_registry
  container_registry_name   = var.container_registry_name
}

# -----------------------------------------------------------------------------
# Kubernetes Module
# -----------------------------------------------------------------------------
module "k8s" {
  source = "./modules/k8s"

  parent_id   = var.parent_id
  tenant_id   = var.tenant_id
  region      = var.region
  name_prefix = local.name_prefix

  # Network
  subnet_id = var.subnet_id

  # Cluster config
  k8s_version            = var.k8s_version
  etcd_cluster_size      = var.etcd_cluster_size
  enable_public_endpoint = var.enable_public_endpoint

  # SSH
  ssh_user_name  = var.ssh_user_name
  ssh_public_key = local.ssh_public_key

  # CPU nodes
  cpu_nodes_count            = var.cpu_nodes_count
  cpu_nodes_platform         = var.cpu_nodes_platform
  cpu_nodes_preset           = var.cpu_nodes_preset
  cpu_disk_type              = var.cpu_disk_type
  cpu_disk_size_gib          = var.cpu_disk_size_gib
  cpu_nodes_assign_public_ip = var.cpu_nodes_assign_public_ip

  # GPU nodes
  gpu_nodes_count_per_group  = var.gpu_nodes_count_per_group
  gpu_node_groups            = var.gpu_node_groups
  gpu_nodes_platform         = local.gpu_nodes_platform
  gpu_nodes_preset           = local.gpu_nodes_preset
  gpu_disk_type              = var.gpu_disk_type
  gpu_disk_size_gib          = var.gpu_disk_size_gib
  gpu_nodes_assign_public_ip = var.gpu_nodes_assign_public_ip
  enable_gpu_cluster         = var.enable_gpu_cluster
  infiniband_fabric          = local.infiniband_fabric
  enable_gpu_taints          = var.enable_gpu_taints
  gpu_nodes_preemptible      = var.gpu_nodes_preemptible
  gpu_nodes_driverfull_image = var.gpu_nodes_driverfull_image
  gpu_drivers_preset         = local.gpu_drivers_preset
  gpu_reservation_ids        = var.gpu_reservation_ids

  # Filestore
  enable_filestore = var.enable_filestore
  filestore_id     = var.enable_filestore ? module.platform.filestore_id : null

  # Note: No explicit depends_on needed - Terraform infers dependencies from:
  #   - subnet_id (waits for subnet)
  #   - filestore_id (waits for filestore if enabled)
  # This allows k8s to start as soon as subnet/filestore are ready,
  # without waiting for PostgreSQL (which takes 5-15 min)
}

# -----------------------------------------------------------------------------
# WireGuard VPN Module (Optional)
# -----------------------------------------------------------------------------
module "wireguard" {
  count  = var.enable_wireguard ? 1 : 0
  source = "./modules/wireguard"

  parent_id   = var.parent_id
  region      = var.region
  name_prefix = local.name_prefix

  # Network
  subnet_id   = var.subnet_id
  wg_network  = var.wireguard_network

  # Instance config
  platform      = var.wireguard_platform
  preset        = var.wireguard_preset
  disk_size_gib = var.wireguard_disk_size_gib

  # WireGuard config
  wg_port    = var.wireguard_port
  ui_port    = var.wireguard_ui_port

  # SSH
  ssh_user_name  = var.ssh_user_name
  ssh_public_key = local.ssh_public_key

  # Note: No explicit depends_on needed - Terraform infers from subnet_id
}
