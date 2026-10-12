resource "terraform_data" "dsvm_configuration_validation" {
  lifecycle {
    precondition {
      condition     = local.platform != null
      error_message = "No default DSVM platform is defined for region ${var.region}. Set platform explicitly. Regions with defaults: ${join(", ", sort(keys(local.region_default_platforms)))}."
    }

    precondition {
      condition     = local.platform == null || local.preset != null
      error_message = "No default DSVM preset is defined for platform ${coalesce(local.platform, "<unset>")}. Set preset explicitly. Platforms with defaults: ${join(", ", sort(keys(local.platform_defaults_by_platform)))}."
    }
  }
}

resource "nebius_compute_v1_instance" "dsvm_instance" {
  parent_id = var.parent_id
  name      = "dsvm-instance"

  boot_disk = {
    attach_mode   = "READ_WRITE"
    existing_disk = nebius_compute_v1_disk.dsvm-boot-disk
  }

  network_interfaces = [
    {
      name              = "eth0"
      subnet_id         = var.subnet_id
      ip_address        = {}
      public_ip_address = {}
    }
  ]

  resources = {
    platform = local.platform
    preset   = local.preset
  }

  cloud_init_user_data = templatefile("./files/dsvm-cloud-init.tftpl", {
    ssh_user_name  = var.ssh_user_name,
    ssh_public_key = local.ssh_public_key,
  })

  depends_on = [terraform_data.dsvm_configuration_validation]
}
