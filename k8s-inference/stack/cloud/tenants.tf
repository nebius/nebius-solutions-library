# Tenant storage: for every tenant with `storage = "managed"` and every GPU region the tenant is in
# (tenants.<name>.regions, default: all), a bucket, a service account and an S3 access key
# (stack/modules/tenant-region). Created with the other cloud resources so that every cluster's platform stage
# reads the same credentials from this one state: a worker writes its region's bucket into the tenant
# namespace, every cluster also gets the hub region's bucket (fleet-placed runs and the control cluster's API
# use it).
locals {
  tenant_regions = merge([for tn, t in local.f.tenants : {
    for rn in coalesce(t.regions, sort(keys(local.f.regions))) : "${tn}/${rn}" => { tenant = tn, region = rn, config = t }
  } if t.storage == "managed"]...)
}

module "tenant_region" {
  for_each       = local.tenant_regions
  source         = "../modules/tenant-region"
  name           = "${local.f.name}-${each.value.tenant}-${each.value.region}"
  tenant         = each.value.tenant
  region         = each.value.region
  project_id     = local.f.regions[each.value.region].project_id
  lifecycle_days = each.value.config.lifecycle_days
  protect_data   = local.f.protect_data
  labels         = merge(local.f.labels, { "serverless2.nebius/tenant" = each.value.tenant })
}

output "tenants" {
  description = "Per tenant: its namespace and its bucket per region (managed storage only)."
  value = { for tn, t in local.f.tenants : tn => {
    namespace = "tenant-${tn}"
    buckets   = { for k, v in local.tenant_regions : v.region => module.tenant_region[k].bucket if v.tenant == tn }
  } }
}

output "tenant_storage" {
  description = "Per region and tenant: bucket, S3 endpoint, region and access key (sensitive; the platform stage writes the tenant Secrets from it)."
  sensitive   = true
  value       = { for rn in keys(local.f.regions) : rn => { for k, v in local.tenant_regions : v.tenant => module.tenant_region[k].storage if v.region == rn } }
}
