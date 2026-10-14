mock_provider "helm" {}
mock_provider "kubernetes" {}
mock_provider "kubectl" {}
mock_provider "random" {}
mock_provider "http" {}
mock_provider "time" {}
mock_provider "units" {}

variables {
  tenant_id = "tenant-e00r7z9vfxmg1bk99s"
  parent_id = "project-e00r7z9vfxmg1bk99s"
  subnet_id = "subnet-e00r7z9vfxmg1bk99s"
  region    = "eu-west1"
  iam_token = "test-token"
  ssh_public_key = {
    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest gb300-test"
  }
  infiniband_fabric = "fabric-4"
  node_group_strategy = {
    max_unavailable = { count = 1 }
    max_surge       = { count = 0 }
  }
  loki = {
    enabled = false
  }
  gb300 = {
    rack_count               = 2
    boot_disk_size_gibibytes = 1024
    local_nvme               = true
  }
}

run "valid_two_rack_production_configuration" {
  command = plan

  plan_options {
    target = [
      terraform_data.gb300_validation,
      nebius_compute_v1_nvl_instance_group.gb300,
      nebius_mk8s_v1_node_group.gb300,
      nebius_mk8s_v1_node_group.gpu,
    ]
  }

  assert {
    condition     = length(nebius_compute_v1_nvl_instance_group.gb300) == 2
    error_message = "A two-rack GB300 configuration must create two NVLink instance groups."
  }

  assert {
    condition     = length(nebius_mk8s_v1_node_group.gb300) == 2
    error_message = "A two-rack GB300 configuration must create two MK8s node groups."
  }

  assert {
    condition     = length(nebius_mk8s_v1_node_group.gpu) == 0
    error_message = "The generic GPU node groups must be disabled when GB300 is enabled."
  }

  assert {
    condition     = contains(keys(nebius_mk8s_v1_node_group.gb300), "rack-001") && contains(keys(nebius_mk8s_v1_node_group.gb300), "rack-002")
    error_message = "Generated GB300 rack keys must be stable and sequential."
  }

  assert {
    condition     = nebius_mk8s_v1_node_group.gb300["rack-001"].template.boot_disk.size_gibibytes == 1024
    error_message = "GB300 node groups must use the configured 1 TiB boot disk."
  }

  assert {
    condition     = nebius_mk8s_v1_node_group.gb300["rack-001"].template.local_disks.passthrough_group.requested
    error_message = "GB300 node groups must request local NVMe passthrough when local_nvme is enabled."
  }

  assert {
    condition     = nebius_mk8s_v1_node_group.gb300["rack-001"].template.local_disks.config.kubelet_ephemeral
    error_message = "GB300 local NVMe must be configured as kubelet ephemeral storage."
  }
}

run "valid_one_rack_production_configuration" {
  command = plan

  variables {
    infiniband_fabric = ""
    gb300 = {
      rack_count = 1
    }
  }

  plan_options {
    target = [
      terraform_data.gb300_validation,
      nebius_compute_v1_nvl_instance_group.gb300,
      nebius_mk8s_v1_node_group.gb300,
    ]
  }

  assert {
    condition     = length(nebius_compute_v1_nvl_instance_group.gb300) == 1
    error_message = "A one-rack GB300 configuration must create one NVLink instance group."
  }

  assert {
    condition     = length(nebius_mk8s_v1_node_group.gb300) == 1
    error_message = "A one-rack GB300 configuration must create one MK8s node group."
  }

  assert {
    condition     = nebius_mk8s_v1_node_group.gb300["rack-001"].template.gpu_cluster == null
    error_message = "A fabricless one-rack GB300 node group must not attach to a GPU cluster."
  }
}

run "reject_fractional_rack_count" {
  command = plan

  variables {
    gb300 = {
      rack_count = 1.5
    }
  }

  plan_options {
    target = [
      terraform_data.gb300_validation,
    ]
  }

  expect_failures = [
    var.gb300,
  ]
}

run "reject_missing_rollout_strategy" {
  command = plan

  variables {
    node_group_strategy = null
  }

  plan_options {
    target = [
      terraform_data.gb300_validation,
    ]
  }

  expect_failures = [
    terraform_data.gb300_validation,
  ]
}

run "reject_nonzero_surge" {
  command = plan

  variables {
    node_group_strategy = {
      max_unavailable = { count = 1 }
      max_surge       = { count = 1 }
    }
  }

  plan_options {
    target = [
      terraform_data.gb300_validation,
    ]
  }

  expect_failures = [
    terraform_data.gb300_validation,
  ]
}

run "reject_missing_infiniband_fabric_for_multiple_racks" {
  command = plan

  variables {
    infiniband_fabric = ""
  }

  plan_options {
    target = [
      terraform_data.gb300_validation,
    ]
  }

  expect_failures = [
    terraform_data.gb300_validation,
  ]
}
