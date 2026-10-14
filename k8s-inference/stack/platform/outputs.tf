output "cluster" {
  value = { id = local.id, region = local.region, roles = local.role, endpoint = local.cluster.endpoint, kubeconfig_command = local.cluster.kubeconfig_command }
}
output "gateway_ip" { value = local.cluster.gateway_ip }
output "hostnames" { value = local.hostnames }
output "api_url" { value = try("https://${local.hostnames.api}", null) } # the control cluster's; null on a GPU region
output "grafana_url" { value = "https://${local.hostnames.grafana}" }
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

output "tenants" {
  description = "Tenants present on this cluster: namespace and bucket (the hub region's on the control cluster)."
  value = { for tn, t in local.tenants : tn => {
    namespace = "tenant-${tn}"
    bucket    = try(local.storage_here[tn].bucket, null)
  } }
}
output "tenant_keys" {
  description = "LiteLLM API keys per tenant and key name (control cluster)."
  sensitive   = true
  value       = local.role.control ? { for tn, t in local.tenants : tn => { for kn, k in t.keys : kn => "sk-${random_password.key["${tn}-${kn}"].result}" } } : null
}
output "fleet_operator_keys" {
  description = "Fleet-operator login keys by name (control cluster); separate from tenant/customer keys."
  sensitive   = true
  value       = local.role.control ? { for name in local.f.fleet_operator_keys : name => "sk-${random_password.key["platform-operator-${name}"].result}" } : null
}
output "acceptance" {
  description = "Inputs of tools/verify.sh (control cluster): the API, the first tenant key, the first admin key, the run class and the example model's definition."
  sensitive   = true
  value       = local.acceptance
}
output "example_model" {
  description = "The acceptance probe's example model, defined through the API (control cluster)."
  value       = local.example_on ? "${local.example_spec.id}: https://${try(local.hostnames.api, "")}/v1/models/${local.example_spec.id}:invoke (console: https://${try(local.hostnames.app, "")}/models)" : "disabled"
}
output "probe" {
  value = local.probe_on ? "acceptance-probe Job in namespace api succeeded (run class ${local.f.acceptance.model}${local.example_on ? ", example model ${local.example_spec.id}" : ""})" : "not run at apply time: tools/verify.sh"
}
