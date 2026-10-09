output "cluster" {
  value = { id = local.id, region = local.region, roles = local.role, endpoint = local.cluster.endpoint, kubeconfig_command = local.cluster.kubeconfig_command }
}
output "gateway_ip" { value = local.cluster.gateway_ip }
output "hostnames" { value = local.hostnames }
output "api_url" { value = "https://${local.hostnames.api}" }
output "grafana_url" { value = "https://${local.hostnames.grafana}" }
output "ui_url" { value = local.role.control ? "https://${local.hostnames.app}" : null }
output "litellm_url" { value = local.role.control ? "https://${local.hostnames.litellm}" : null }
output "tenant_namespace_prefix" { value = "tenant-" }
output "kueue_namespace" { value = "kueue-system" }

output "multikueue_kubeconfig" {
  sensitive = true
  value     = try(local.worker_kubeconfig["multikueue"], null)
}
output "api_agent_kubeconfig" {
  sensitive = true
  value     = try(local.worker_kubeconfig["api-agent"], null)
}
output "credentials" {
  description = "Operator credentials (control cluster)."
  sensitive   = true
  value = local.role.control ? {
    grafana_admin_user     = "admin"
    grafana_admin_password = local.secrets.grafana_admin_password
    litellm_master_key     = local.secrets.litellm_master_key
  } : null
}
output "litellm_master_key" {
  sensitive = true
  value     = local.role.control ? local.secrets.litellm_master_key : null
}
