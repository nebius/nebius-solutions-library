locals {
  fleet   = jsondecode(read_tfvars_file(get_env("K8S_FLEET_CONFIG", "${get_terragrunt_dir()}/terraform.tfvars")))
  backend = jsondecode(read_tfvars_file(get_env("K8S_FLEET_BACKEND")))
  source  = get_env("K8S_TERRAFORM_SOURCE")
  prefix  = get_env("K8S_FLEET_STATE_PREFIX", "k8s-fleet")
}

unit "cluster" {
  expansion {
    for_each = local.fleet.clusters
  }
  source = "${get_terragrunt_dir()}/unit"
  path   = "clusters/${each.key}"
  values = {
    source  = local.source
    backend = merge(local.backend, { key = "${local.prefix}/${each.key}/terraform.tfstate" })
    inputs = merge(
      { cluster_name = each.key },
      local.fleet.defaults, each.value
    )
  }
}
