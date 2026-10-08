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

output "endpoints" {
  value = { for mid, e in(local.role.worker ? try(local.endpoints_by_cluster[local.id], {}) : {}) : mid => "https://${mid}-predictor.models.${trimprefix(local.platform.api_url, "https://api.")}" }
}

output "api_url" { value = local.platform.api_url }
output "probe" { value = local.probe_on ? "acceptance-probe Job in namespace api succeeded (model ${local.f.acceptance.model}${local.f.acceptance.endpoint != null ? ", endpoint ${local.f.acceptance.endpoint}" : ""})" : "disabled" }
