locals {
  regions_default = {
    eu-west1 = {
      gateway_subnet_cidr = "10.48.0.0/20"
      private_subnet_cidr = "10.48.16.0/20"
    }
    eu-north1 = {
      gateway_subnet_cidr = "10.0.0.0/20"
      private_subnet_cidr = "10.0.16.0/20"
    }
    eu-north2 = {
      gateway_subnet_cidr = "10.24.0.0/20"
      private_subnet_cidr = "10.24.16.0/20"
    }
    us-central1 = {
      gateway_subnet_cidr = "10.96.0.0/20"
      private_subnet_cidr = "10.96.16.0/20"
    }
    me-west1 = {
      gateway_subnet_cidr = "10.144.0.0/20"
      private_subnet_cidr = "10.144.16.0/20"
    }
    uk-south1 = {
      gateway_subnet_cidr = "10.64.0.0/20"
      private_subnet_cidr = "10.64.16.0/20"
    }
  }

  current_region_defaults = local.regions_default[var.region]

  gateway_subnet_cidr = coalesce(var.gateway_subnet_cidr, local.current_region_defaults.gateway_subnet_cidr)
  private_subnet_cidr = coalesce(var.private_subnet_cidr, local.current_region_defaults.private_subnet_cidr)

  ssh_public_key = var.ssh_public_key.key != null ? var.ssh_public_key.key : (
  fileexists(var.ssh_public_key.path) ? file(var.ssh_public_key.path) : null)
}