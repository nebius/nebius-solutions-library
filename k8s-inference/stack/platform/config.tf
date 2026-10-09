# The fleet definition, shared with every other stage through the `config` module (stack/config): the
# schema, its defaults and validations, and the derived per-cluster view live there once. This file is
# the same in every stage.

variable "fleet" {
  description = "The inference fleet (terraform.tfvars). The schema is stack/config/variables.tf."
  type        = any
}

# Stage selector (platform and models run once per cluster). The wrapper stack.sh sets it; cloud ignores it.
variable "target" {
  description = "Cluster id this stage applies to (a region id or `control`)."
  type        = string
  default     = ""
}

module "config" {
  source = "../config"
  fleet  = var.fleet
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
}

# The Nebius IAM access token (12 h) for the Kubernetes and Helm providers of this cluster; stack.sh sets
# TF_VAR_iam_token from NEBIUS_IAM_TOKEN (`nebius iam get-access-token`), exactly as k8s-training does.
variable "iam_token" {
  type      = string
  sensitive = true
}
