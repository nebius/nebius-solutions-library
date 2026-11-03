mock_provider "nebius" {}
mock_provider "local" {}

variables {
  parent_id         = "project-e00000000000000000"
  subnet_id         = "vpcsubnet-e00000000000000000"
  region            = "eu-north1"
  platform          = "cpu-d3"
  preset            = "4vcpu-16gb"
  boot_image_family = "ubuntu24.04"
  users             = []
}

run "regular_instance_upgrades_without_prompts" {
  command = plan

  module {
    source = "../modules/instance"
  }

  variables {
    preemptible = false
  }

  assert {
    condition     = yamldecode(nebius_compute_v1_instance.instance.cloud_init_user_data).runcmd[0] == "apt-get update"
    error_message = "Package indexes must be refreshed before upgrading."
  }

  assert {
    condition     = yamldecode(nebius_compute_v1_instance.instance.cloud_init_user_data).runcmd[1] == "DEBIAN_FRONTEND=noninteractive apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold upgrade"
    error_message = "Package upgrades must accept confirmation and resolve package configuration prompts without user input."
  }
}

run "preemptible_instance_upgrades_without_prompts" {
  command = plan

  module {
    source = "../modules/instance"
  }

  variables {
    preemptible = true
  }

  assert {
    condition     = yamldecode(nebius_compute_v1_instance.instance.cloud_init_user_data).runcmd[0] == "apt-get update"
    error_message = "Package indexes must be refreshed before upgrading."
  }

  assert {
    condition     = yamldecode(nebius_compute_v1_instance.instance.cloud_init_user_data).runcmd[1] == "DEBIAN_FRONTEND=noninteractive apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold upgrade"
    error_message = "Package upgrades must accept confirmation and resolve package configuration prompts without user input."
  }
}
