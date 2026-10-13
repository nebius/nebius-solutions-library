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
}

# Short names, the ones this stage uses.
locals {
  f           = module.config.fleet
  hub_region  = module.config.hub_region
  hub_project = module.config.hub_project
  hub_id      = module.config.hub_id
  clusters    = module.config.clusters
  roles       = module.config.roles
  projects    = module.config.projects
  images_host = module.config.images_host
}
