# =============================================================================
# Output Values
# =============================================================================

# -----------------------------------------------------------------------------
# Network Outputs
# -----------------------------------------------------------------------------
output "network_id" {
  description = "VPC network ID"
  value       = module.platform.network_id
}

output "subnet_id" {
  description = "VPC subnet ID"
  value       = module.platform.subnet_id
}

# -----------------------------------------------------------------------------
# Kubernetes Outputs
# -----------------------------------------------------------------------------
output "cluster_id" {
  description = "Kubernetes cluster ID"
  value       = module.k8s.cluster_id
}

output "cluster_name" {
  description = "Kubernetes cluster name"
  value       = module.k8s.cluster_name
}

output "cluster_endpoint" {
  description = "Kubernetes API endpoint"
  value       = module.k8s.cluster_endpoint
}

output "cluster_ca_certificate" {
  description = "Kubernetes cluster CA certificate"
  value       = module.k8s.cluster_ca_certificate
  sensitive   = true
}

# -----------------------------------------------------------------------------
# Storage Outputs
# -----------------------------------------------------------------------------
output "storage_bucket" {
  description = "Object storage bucket details"
  value = {
    name     = module.platform.storage_bucket_name
    endpoint = module.platform.storage_endpoint
    region   = module.platform.storage_region
  }
}

output "storage_credentials" {
  description = "S3-compatible storage credentials"
  value = {
    access_key_id     = module.platform.storage_access_key_id
    secret_access_key = module.platform.storage_secret_access_key
  }
  sensitive = true
}

output "storage_secret_reference_id" {
  description = "MysteryBox secret reference ID for storage credentials (use nebius mysterybox CLI to retrieve)"
  value       = module.platform.storage_secret_reference_id
}

output "filestore" {
  description = "Shared filesystem details"
  value = var.enable_filestore ? {
    id        = module.platform.filestore_id
    mount_tag = "data"
  } : null
}

# -----------------------------------------------------------------------------
# Database Outputs (Nebius Managed PostgreSQL)
# -----------------------------------------------------------------------------
output "enable_managed_postgresql" {
  description = "Whether managed PostgreSQL is enabled"
  value       = var.enable_managed_postgresql
}

output "postgresql" {
  description = "PostgreSQL connection details (null if using in-cluster PostgreSQL)"
  value = var.enable_managed_postgresql ? {
    host     = module.platform.postgresql_host
    port     = module.platform.postgresql_port
    database = module.platform.postgresql_database
    username = module.platform.postgresql_username
  } : null
}

output "postgresql_password" {
  description = "PostgreSQL admin password (null if using MysteryBox or in-cluster PostgreSQL)"
  value       = var.enable_managed_postgresql ? module.platform.postgresql_password : null
  sensitive   = true
}

# -----------------------------------------------------------------------------
# MysteryBox Secret IDs
# -----------------------------------------------------------------------------
output "mysterybox_secrets" {
  description = "MysteryBox secret IDs (if configured)"
  value = {
    postgresql_secret_id = module.platform.postgresql_mysterybox_secret_id
    mek_secret_id        = module.platform.mek_mysterybox_secret_id
  }
}

# -----------------------------------------------------------------------------
# Container Registry Outputs
# -----------------------------------------------------------------------------
output "container_registry" {
  description = "Container Registry details"
  value = var.enable_container_registry ? {
    id       = module.platform.container_registry_id
    name     = module.platform.container_registry_name
    endpoint = module.platform.container_registry_endpoint
  } : null
}

# -----------------------------------------------------------------------------
# WireGuard Outputs (if enabled)
# -----------------------------------------------------------------------------
output "wireguard" {
  description = "WireGuard VPN details"
  value = var.enable_wireguard ? {
    public_ip  = module.wireguard[0].public_ip
    private_ip = module.wireguard[0].private_ip
    ui_url     = module.wireguard[0].ui_url
    ssh        = module.wireguard[0].ssh_command
  } : null
}

# -----------------------------------------------------------------------------
# GPU Configuration Outputs
# -----------------------------------------------------------------------------
output "gpu_nodes_driverfull_image" {
  description = "Whether GPU nodes use driverfull images with pre-installed drivers"
  value       = var.gpu_nodes_driverfull_image
}

output "gpu_nodes_platform" {
  description = "GPU platform type (e.g. gpu-h100-sxm, gpu-h200-sxm)"
  value       = local.gpu_nodes_platform
}

# -----------------------------------------------------------------------------
# Connection Instructions
# -----------------------------------------------------------------------------
output "next_steps" {
  description = "Next steps after deployment"
  value       = <<-EOT
    
    ========================================
    OSMO on Nebius - Deployment Complete
    ========================================
    
    1. Get Kubernetes credentials:
       nebius mk8s cluster get-credentials --id ${module.k8s.cluster_id} --external
    
    2. Verify cluster access:
       kubectl get nodes
    
    ${var.enable_wireguard ? "3. Set up WireGuard VPN:\n       cd ../000-prerequisites && ./wireguard-client-setup.sh\n       WireGuard UI: ${module.wireguard[0].ui_url}\n    \n    4. " : "3. "}Deploy OSMO components:
       cd ../002-setup
       ./01-deploy-gpu-infrastructure.sh
       ./02-deploy-observability.sh
       ./03-deploy-nginx-ingress.sh
       ./03b-enable-tls.sh <hostname>   # optional, recommended; omit <hostname> to use OSMO_INGRESS_HOSTNAME
       ./04-deploy-osmo-control-plane.sh
       ./05-deploy-osmo-backend.sh
       ./06-configure-storage.sh
    
    ${var.enable_managed_postgresql ? "PostgreSQL Connection (Managed):\n       Host: ${module.platform.postgresql_host}\n       Port: ${module.platform.postgresql_port}\n       Database: ${module.platform.postgresql_database}\n       Username: ${module.platform.postgresql_username}" : "PostgreSQL: Using in-cluster PostgreSQL (deployed via Helm in 05-deploy-osmo-control-plane.sh)"}
    
    Object Storage:
       Bucket: ${module.platform.storage_bucket_name}
       Endpoint: ${module.platform.storage_endpoint}
    
    ${var.enable_container_registry ? "Container Registry:\n       Name: ${module.platform.container_registry_name}\n       Endpoint: ${module.platform.container_registry_endpoint}\n       Docker login: docker login ${module.platform.container_registry_endpoint}" : "Container Registry: Disabled (set enable_container_registry = true to enable)"}
    
  EOT
}
