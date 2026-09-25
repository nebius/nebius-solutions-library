locals {
  release-suffix = random_string.release_suffix.result
  config         = yamldecode(file("${terraform.workspace}.yaml"))
}

resource "random_string" "release_suffix" {
  keepers = {
    ami_id = "${var.parent_id}"
  }
  length  = 6
  upper   = false
  lower   = true
  numeric = true
  special = false
}


