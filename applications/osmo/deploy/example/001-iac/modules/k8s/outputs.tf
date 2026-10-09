# =============================================================================
# Kubernetes Module Outputs
# =============================================================================

output "cluster_id" {
  description = "Kubernetes cluster ID"
  value       = nebius_mk8s_v1_cluster.main.id
}

output "cluster_name" {
  description = "Kubernetes cluster name"
  value       = nebius_mk8s_v1_cluster.main.name
}

output "cluster_endpoint" {
  description = "Kubernetes API endpoint"
  value = var.enable_public_endpoint ? (
    nebius_mk8s_v1_cluster.main.status.control_plane.endpoints.public_endpoint
  ) : (
    try(nebius_mk8s_v1_cluster.main.status.control_plane.endpoints.private_endpoint, "")
  )
}

output "cluster_ca_certificate" {
  description = "Kubernetes cluster CA certificate"
  value       = nebius_mk8s_v1_cluster.main.status.control_plane.auth.cluster_ca_certificate
  sensitive   = true
}

output "service_account_id" {
  description = "Service account ID for node groups"
  value       = nebius_iam_v1_service_account.k8s_nodes.id
}

output "gpu_cluster_id" {
  description = "GPU cluster ID"
  value       = var.enable_gpu_cluster && var.gpu_nodes_count_per_group > 0 ? nebius_compute_v1_gpu_cluster.main[0].id : null
}
