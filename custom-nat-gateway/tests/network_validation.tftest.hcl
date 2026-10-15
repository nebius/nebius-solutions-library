mock_provider "nebius" {}

variables {
  parent_id      = "project-test"
  vpc_network_id = "vpcnetwork-test"
  ssh_public_key = { key = "ssh-ed25519 test-key offline-test" }
}

override_data {
  target = data.nebius_vpc_v1_network.existing_network
  values = {
    id        = "vpcnetwork-test"
    parent_id = "project-test"
  }
}

run "matching_project" {
  command = plan

  assert {
    condition     = length(nebius_compute_v1_instance.workload_vm) == 1
    error_message = "The default configuration must include a workload VM."
  }
}

run "gateway_only" {
  command = plan

  variables {
    deploy_test_vm = false
  }

  assert {
    condition     = length(nebius_compute_v1_instance.workload_vm) == 0
    error_message = "Gateway-only mode must not create a workload VM."
  }
}

run "wrong_project" {
  command = plan

  override_data {
    target = data.nebius_vpc_v1_network.existing_network
    values = {
      id        = "vpcnetwork-test"
      parent_id = "project-other"
    }
  }

  expect_failures = [terraform_data.network_validation]
}
