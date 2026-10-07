terraform {
  required_providers {
    nebius = {
      source = "nebius/nebius"
    }

    units = {
      source = "dstaroff/units"
    }
  }
}

module "labels" {
  source = "../labels"
}

module "resources" {
  source = "../available_resources"
}
