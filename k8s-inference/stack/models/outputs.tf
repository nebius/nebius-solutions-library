output "tenants" {
  value = { for tn, t in local.tenants : tn => {
    namespace = "tenant-${tn}"
    bucket    = "${local.name}-${tn}-${local.role.worker ? local.region : local.hub_region}" # the primary bucket of a control-only tenant is the hub's
  } }
}

output "tenant_storage" {
  description = "Per tenant: this region's bucket and credentials (read by the other clusters' models stages)."
  sensitive   = true
  value       = local.storage_here
}

output "tenant_keys" {
  description = "LiteLLM API keys per tenant and key name (control cluster)."
  sensitive   = true
  value       = { for tn, t in local.tenants : tn => { for kn, k in t.keys : kn => "sk-${random_password.key["${tn}-${kn}"].result}" } if local.role.control }
}

output "example_model" {
  description = "The acceptance probe's example model, defined through the API (control cluster)."
  value       = local.example_on ? "${local.example_spec.id}: ${local.platform.api_url}/v1/models/${local.example_spec.id}:invoke (console: ${replace(local.platform.api_url, "https://api.", "https://app.")}/models)" : "disabled"
}

output "api_url" { value = local.platform.api_url }
output "probe" { value = local.probe_on ? "acceptance-probe Job in namespace api succeeded (run class ${local.f.acceptance.model}${local.example_on ? ", example model ${local.example_spec.id}" : ""})" : "disabled" }
