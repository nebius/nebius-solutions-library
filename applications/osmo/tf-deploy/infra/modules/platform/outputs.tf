# =============================================================================
# Platform Module Outputs
# =============================================================================

# -----------------------------------------------------------------------------
# Network Outputs
# -----------------------------------------------------------------------------
output "network_id" {
  description = "VPC network ID"
  value       = var.network_id
}

output "subnet_id" {
  description = "VPC subnet ID"
  value       = var.subnet_id
}

# -----------------------------------------------------------------------------
# Storage Outputs
# -----------------------------------------------------------------------------
output "storage_bucket_name" {
  description = "Storage bucket name"
  value       = nebius_storage_v1_bucket.main.name
}

output "storage_endpoint" {
  description = "S3-compatible storage endpoint (dynamic from region)"
  value       = "https://storage.${var.region}.nebius.cloud"
}

# Legacy TOS-format endpoint retained for compatibility with older experiments.
# The example setup scripts configure workflow storage with s3://<bucket> plus
# override_url=https://storage.<region>.nebius.cloud.
output "storage_tos_endpoint" {
  description = "TOS-format endpoint for OSMO workflow configuration"
  value       = "tos://storage.${var.region}.nebius.cloud/${nebius_storage_v1_bucket.main.name}"
}

output "storage_access_key_id" {
  description = "Storage access key ID"
  value       = nebius_iam_v2_access_key.storage.status.aws_access_key_id
  sensitive   = true
}

# Storage secret is ephemeral (not stored in state) - retrieve via CLI:
#   nebius mysterybox v1 payload get-by-key \
#     --secret-id $(terraform output -raw storage_secret_reference_id) \
#     --key secret --format json | jq -r '.data.string_value'
output "storage_secret_access_key" {
  description = "Storage secret access key - use CLI command above to retrieve (ephemeral, not in state)"
  value       = null # Ephemeral values cannot be output; use MysteryBox CLI
  sensitive   = true
}

# MysteryBox secret reference ID (for external secret management tools)
output "storage_secret_reference_id" {
  description = "MysteryBox secret reference ID for storage credentials"
  value       = nebius_iam_v2_access_key.storage.status.secret_reference_id
}

# -----------------------------------------------------------------------------
# Filestore Outputs
# -----------------------------------------------------------------------------
output "filestore_id" {
  description = "Filestore ID"
  value       = var.enable_filestore ? nebius_compute_v1_filesystem.shared[0].id : null
}

output "filestore_size_bytes" {
  description = "Filestore size in bytes"
  value       = var.enable_filestore ? nebius_compute_v1_filesystem.shared[0].size_bytes : null
}

# -----------------------------------------------------------------------------
# PostgreSQL Outputs (Nebius Managed Service)
# -----------------------------------------------------------------------------
output "enable_managed_postgresql" {
  description = "Whether managed PostgreSQL is enabled"
  value       = var.enable_managed_postgresql
}

output "postgresql_host" {
  description = "PostgreSQL host (null if using in-cluster PostgreSQL)"
  value       = var.enable_managed_postgresql ? nebius_msp_postgresql_v1alpha1_cluster.main[0].status.connection_endpoints.private_read_write : null
}

output "postgresql_port" {
  description = "PostgreSQL port"
  value       = 5432
}

output "postgresql_database" {
  description = "PostgreSQL database name"
  value       = var.enable_managed_postgresql ? nebius_msp_postgresql_v1alpha1_cluster.main[0].bootstrap.db_name : var.postgresql_database_name
}

output "postgresql_username" {
  description = "PostgreSQL username"
  value       = var.enable_managed_postgresql ? nebius_msp_postgresql_v1alpha1_cluster.main[0].bootstrap.user_name : var.postgresql_username
}

output "postgresql_password" {
  description = "PostgreSQL password (null - always use MysteryBox to retrieve)"
  # Note: Password is stored in MysteryBox and cannot be output directly.
  # Use the CLI to retrieve: nebius mysterybox v1 payload get-by-key --secret-id <id> --key password
  value     = null
  sensitive = true
}

output "postgresql_mysterybox_secret_id" {
  description = "MysteryBox secret ID for PostgreSQL password"
  value       = var.enable_managed_postgresql ? nebius_mysterybox_v1_secret.postgresql_password[0].id : null
}

output "mek_mysterybox_secret_id" {
  description = "MysteryBox secret ID for MEK"
  value       = nebius_mysterybox_v1_secret.mek.id
}

# -----------------------------------------------------------------------------
# Container Registry Outputs
# -----------------------------------------------------------------------------
output "enable_container_registry" {
  description = "Whether Container Registry is enabled"
  value       = var.enable_container_registry
}

output "container_registry_id" {
  description = "Container Registry ID"
  value       = var.enable_container_registry ? nebius_registry_v1_registry.main[0].id : null
}

output "container_registry_name" {
  description = "Container Registry name"
  value       = var.enable_container_registry ? nebius_registry_v1_registry.main[0].name : null
}

output "container_registry_endpoint" {
  description = "Container Registry endpoint for docker login/push"
  value       = var.enable_container_registry ? nebius_registry_v1_registry.main[0].status.registry_fqdn : null
}
