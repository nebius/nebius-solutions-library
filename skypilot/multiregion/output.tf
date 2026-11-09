
output "kube_cluster" {
  description = "Kubernetes clusters info by region key."
  value = {
    for cluster_key, cluster in nebius_mk8s_v1_cluster.k8s-cluster : cluster_key => {
      id        = cluster.id
      name      = cluster.name
      endpoints = cluster.status.control_plane.endpoints
    }
  }
}

output "grafana_password" {
  sensitive = true
  value     = module.o11y.nebius_grafana_password
}

output "grafana_service_account" {
  description = "Grafana service account information."
  sensitive   = true
  value       = module.o11y.grafana_service_account
}
output "kube_cluster_ca_certificate" {
  sensitive = true
  value     = nebius_mk8s_v1_cluster.k8s-cluster[local.primary-cluster-key].status.control_plane.auth.cluster_ca_certificate
}

output "shared-filesystem" {
  description = "Shared filesystems by region key."
  value       = local.shared-filesystem
}

output "mlflow_cluster" {
  description = "Managed Service for MLflow cluster details (null when disabled)."
  value = var.enable_mlflow_cluster ? {
    id                 = nebius_msp_mlflow_v1alpha1_cluster.mlflow[0].id
    name               = nebius_msp_mlflow_v1alpha1_cluster.mlflow[0].name
    parent_id          = nebius_msp_mlflow_v1alpha1_cluster.mlflow[0].parent_id
    public_access      = nebius_msp_mlflow_v1alpha1_cluster.mlflow[0].public_access
    service_account_id = nebius_msp_mlflow_v1alpha1_cluster.mlflow[0].service_account_id
    status             = try(nebius_msp_mlflow_v1alpha1_cluster.mlflow[0].status, null)
  } : null
}

output "mlflow_admin_username" {
  description = "MLflow admin username (null when MLflow is disabled)."
  value       = var.enable_mlflow_cluster ? nebius_msp_mlflow_v1alpha1_cluster.mlflow[0].admin_username : null
}

output "mlflow_admin_password" {
  description = "MLflow admin password (sensitive; null when MLflow is disabled)."
  value       = var.enable_mlflow_cluster ? coalesce(var.mlflow_admin_password, try(random_password.mlflow_admin_password[0].result, null)) : null
  sensitive   = true
}

output "mlflow_status" {
  description = "MLflow cluster status object (null when MLflow is disabled)."
  value       = var.enable_mlflow_cluster ? try(nebius_msp_mlflow_v1alpha1_cluster.mlflow[0].status, null) : null
}
