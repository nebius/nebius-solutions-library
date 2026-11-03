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

run "instance_upgrades_via_cloud_init" {
  command = plan

  module {
    source = "../modules/instance"
  }

  assert {
    condition     = yamldecode(nebius_compute_v1_instance.instance.cloud_init_user_data).package_upgrade == true
    error_message = "Package upgrades must use cloud-init's package_upgrade so they run unattended."
  }
}

run "instance_holds_nvidia_and_cuda_packages" {
  command = plan

  module {
    source = "../modules/instance"
  }

  assert {
    condition = anytrue([
      for c in yamldecode(nebius_compute_v1_instance.instance.cloud_init_user_data).bootcmd :
      strcontains(c, "apt-mark hold") && strcontains(c, "nvidia-*") && strcontains(c, "cuda-*")
    ])
    error_message = "NVIDIA driver and CUDA packages must be held before the upgrade runs."
  }
}
