output "namespace" {
  description = "OSMO namespace."
  value       = var.namespace
}

output "ingress_hostname" {
  description = "Browser-facing OSMO hostname."
  value       = var.ingress_hostname
}

output "auth_domain" {
  description = "Keycloak hostname used by the auth-enabled OSMO releases."
  value       = local.auth_domain
}

output "keycloak_external_url" {
  description = "External Keycloak URL used by the OSMO releases."
  value       = local.keycloak_external_url
}

output "keycloak_admin_password" {
  description = "Keycloak admin password managed by Terraform."
  value       = local.keycloak_admin_password
  sensitive   = true
}

output "oidc_client_secret" {
  description = "Client secret for the Keycloak osmo-browser-flow client."
  value       = local.oidc_client_secret
  sensitive   = true
}

output "service_base_url" {
  description = "service_base_url written for workflow execution."
  value       = local.service_base_url_value
}

output "monitoring_namespace" {
  description = "Namespace for the observability stack."
  value       = var.monitoring_namespace
}

output "grafana_admin_password" {
  description = "Grafana admin password managed by Terraform."
  value       = var.deploy_observability ? local.grafana_admin_password_value : null
  sensitive   = true
}

output "backend_operator_namespace" {
  description = "Namespace for the OSMO backend operator."
  value       = var.backend_operator_namespace
}

output "workflows_namespace" {
  description = "Namespace used for workflow execution."
  value       = var.workflows_namespace
}

output "backend_name" {
  description = "Backend name registered in OSMO."
  value       = var.backend_name
}
