mock_provider "nebius" {}
mock_provider "local" {}

variables {
  parent_id         = "project-e00r7z9vfxmg1bk99s"
  subnet_id         = "vpcsubnet-e00r7z9vfxmg1bk99s"
  region            = "eu-north1"
  platform          = "cpu-d3"
  preset            = "4vcpu-16gb"
  boot_image_family = "ubuntu24.04"
  users             = []
}

run "preemptible_instance_follows_spot_price" {
  command = plan

  module {
    source = "../modules/instance"
  }

  variables {
    preemptible = true
  }

  assert {
    condition     = nebius_compute_v1_instance.instance.follows_spot_price != null
    error_message = "Preemptible instances must explicitly follow the spot price."
  }

  # STOP, priority 3, and FAIL are explicitly set in modules/instance/main.tf, not API defaults.
  # Preserve these existing settings when adding spot pricing.
  assert {
    condition     = try(nebius_compute_v1_instance.instance.preemptible.on_preemption == "STOP" && nebius_compute_v1_instance.instance.preemptible.priority == 3, false)
    error_message = "Preemptible instances must retain STOP behavior and priority 3."
  }

  assert {
    condition     = nebius_compute_v1_instance.instance.recovery_policy == "FAIL"
    error_message = "Preemptible instances must retain the FAIL recovery policy."
  }
}

run "regular_instance_keeps_on_demand_configuration" {
  command = plan

  module {
    source = "../modules/instance"
  }

  variables {
    preemptible = false
  }

  assert {
    condition     = nebius_compute_v1_instance.instance.follows_spot_price == null
    error_message = "Regular instances must not opt into spot pricing."
  }

  assert {
    condition     = nebius_compute_v1_instance.instance.preemptible == null
    error_message = "Regular instances must not include preemption settings."
  }

  assert {
    condition     = nebius_compute_v1_instance.instance.recovery_policy == "RECOVER"
    error_message = "Regular instances must retain the RECOVER recovery policy."
  }
}
