output "namespace" {
  description = "OSMO namespace."
  value       = module.app.namespace
}

output "ingress_hostname" {
  description = "Browser-facing OSMO hostname."
  value       = module.app.ingress_hostname
}

output "auth_domain" {
  description = "Keycloak hostname used by the auth-enabled OSMO releases."
  value       = module.app.auth_domain
}

output "keycloak_external_url" {
  description = "External Keycloak URL used by the OSMO releases."
  value       = module.app.keycloak_external_url
}

output "keycloak_admin_password" {
  description = "Keycloak admin password managed by Terraform."
  value       = module.app.keycloak_admin_password
  sensitive   = true
}

output "oidc_client_secret" {
  description = "Client secret for the Keycloak osmo-browser-flow client."
  value       = module.app.oidc_client_secret
  sensitive   = true
}

output "service_base_url" {
  description = "service_base_url written for workflow execution."
  value       = module.app.service_base_url
}

output "monitoring_namespace" {
  description = "Namespace for the observability stack."
  value       = module.app.monitoring_namespace
}

output "grafana_admin_password" {
  description = "Grafana admin password managed by Terraform."
  value       = module.app.grafana_admin_password
  sensitive   = true
}

output "backend_operator_namespace" {
  description = "Namespace for the OSMO backend operator."
  value       = module.app.backend_operator_namespace
}

output "workflows_namespace" {
  description = "Namespace used for workflow execution."
  value       = module.app.workflows_namespace
}

output "backend_name" {
  description = "Backend name registered in OSMO."
  value       = module.app.backend_name
}
