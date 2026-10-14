# The fleet definition, shared with every other stage through the `config` module (stack/config): the
# schema, its defaults and validations, and the derived per-cluster view live there once. This file is
# the same in every stage.

variable "fleet" {
  description = "The inference fleet (terraform.tfvars). The schema is stack/config/variables.tf."
  type        = any
}

# Stage selector (the platform stage runs once per cluster). The wrapper stack.sh sets it; cloud ignores it.
variable "target" {
  description = "Cluster id this stage applies to (a region id or `control`)."
  type        = string
  default     = ""
}

module "config" {
  source = "../config"
  fleet  = var.fleet
  cloud  = { clusters = local.cloud.clusters, hub = local.cloud.hub } # what charts/platform's values depend on (providers.tf)
}

# Short names, as the stage files use them.
locals {
  f                           = module.config.fleet
  hub_region                  = module.config.hub_region
  hub_project                 = module.config.hub_project
  dedicated                   = module.config.dedicated
  region_clusters             = module.config.region_clusters
  hub_id                      = module.config.hub_id
  control_cluster             = module.config.control_cluster
  clusters                    = module.config.clusters
  control_id                  = module.config.control_id
  manager_id                  = module.config.manager_id
  cluster_ids                 = module.config.cluster_ids
  roles                       = module.config.roles
  projects                    = module.config.projects
  state_bucket                = module.config.state_bucket
  state_region                = module.config.state_region
  state_endpoint              = module.config.state_endpoint
  images_host                 = module.config.images_host
  chart_prices                = module.config.chart_prices
  chart_fleet                 = module.config.chart_fleet
  gpu_classes                 = module.config.gpu_classes
  plan                        = module.config.plan
  catalog_dir                 = module.config.catalog_dir
  bundled_catalog             = module.config.bundled_catalog
  catalog_enabled             = module.config.catalog_enabled
  catalog_raw                 = module.config.catalog_raw
  catalog                     = module.config.catalog
  catalog_without_fleet_class = module.config.catalog_without_fleet_class
  catalog_by_cluster          = module.config.catalog_by_cluster
  prepull_by_cluster          = module.config.prepull_by_cluster
  image                       = module.config.image
  images_cache                = module.config.images_cache
  registry                    = module.config.registry_prefix # alias -> image prefix: through the cache, or the registry itself
  host_of                     = module.config.host_of
  hostnames_of                = module.config.hostnames_of
  control_host                = module.config.control_host
  grafana_urls                = module.config.grafana_urls
  litellm_url_in_cluster      = module.config.litellm_url_in_cluster
  trust_volumes               = module.config.trust_volumes
  trust_mounts                = module.config.trust_mounts
  platform_values             = module.config.platform_values[var.target]
}

# The Nebius IAM access token for the Kubernetes and Helm providers of this cluster; stack.sh sets
# TF_VAR_iam_token from NEBIUS_IAM_TOKEN (`nebius iam get-access-token`), exactly as k8s-training does.
variable "iam_token" {
  type      = string
  sensitive = true
}

# Secret inputs named in terraform.tfvars `secrets` (the names of environment variables): stack.sh exports each named
# variable as TF_VAR_<input> and refuses to plan, apply or destroy this stage when a named variable is unset. A feature is off only when its name
# is set to "" in the tfvars, never because a variable happened to be missing from the shell (an apply without the
# NGC key used to delete the NGC Secrets and the cache's nvcr.io credentials on every worker).
variable "ngc_api_key" {
  type      = string
  sensitive = true
  default   = null
}

variable "hf_token" {
  type      = string
  sensitive = true
  default   = null
}

variable "registry_credentials" {
  type      = string
  sensitive = true
  default   = null
}

# The image cache's static Container Registry key (secrets.registry_key_env, default REGISTRY_STATIC_KEY): issued
# once per fleet by the operator with stack/scripts/registry-static-key.sh, never by Terraform. Until api 0.11.78 an
# `external` data source issued it during `terraform plan` when its gitignored cache file was missing (a fresh
# checkout, a second worktree): a read-only plan minted a real credential, two clusters planned in the same minute
# collided on the key's name, and every cluster's plan proposed its own rotation of registry/zot-sync-credentials.
variable "registry_key" {
  type      = string
  sensitive = true
  default   = null
}
