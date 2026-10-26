mock_provider "helm" {}
mock_provider "kubernetes" {}
mock_provider "kubectl" {}
mock_provider "random" {}
mock_provider "http" {}
mock_provider "time" {}
mock_provider "units" {}

variables {
  tenant_id   = "tenant-test-only"
  parent_id   = "project-test-only"
  subnet_id   = "vpcsubnet-test-only"
  region      = "eu-west1"
  iam_token   = "test-token"
  k8s_version = "1.35"
  ssh_public_key = {
    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest preemptible-pricing"
  }
  gpu_nodes_platform = "gpu-h200-sxm"
  gpu_nodes_preset   = "8gpu-128vcpu-1600gb"
  loki = {
    enabled = false
  }
}

run "regular_gpu_nodes_keep_implicit_on_demand_default" {
  command = plan

  plan_options {
    target = [nebius_mk8s_v1_node_group.gpu]
  }

  assert {
    condition = (
      nebius_mk8s_v1_node_group.gpu[0].template.on_demand == null &&
      nebius_mk8s_v1_node_group.gpu[0].template.preemptible == null &&
      nebius_mk8s_v1_node_group.gpu[0].template.follows_spot_price == null &&
      nebius_mk8s_v1_node_group.gpu[0].template.spot_pricing_policy == null
    )
    error_message = "Regular GPU node groups must retain the implicit on-demand default."
  }
}

run "on_demand_gpu_nodes_preserve_capacity_reservation" {
  command = plan

  variables {
    gpu_nodes_reservation_policy = {
      policy = "STRICT"
    }
  }

  plan_options {
    target = [nebius_mk8s_v1_node_group.gpu]
  }

  assert {
    condition = (
      nebius_mk8s_v1_node_group.gpu[0].template.on_demand == null &&
      nebius_mk8s_v1_node_group.gpu[0].template.preemptible == null &&
      nebius_mk8s_v1_node_group.gpu[0].template.follows_spot_price == null &&
      nebius_mk8s_v1_node_group.gpu[0].template.spot_pricing_policy == null &&
      nebius_mk8s_v1_node_group.gpu[0].template.reservation_policy.policy == "STRICT"
    )
    error_message = "Capacity-reserved GPU nodes must retain implicit on-demand pricing and their reservation policy."
  }
}

run "preemptible_gpu_nodes_follow_spot_by_default" {
  command = plan

  variables {
    gpu_nodes_preemptible = true
  }

  plan_options {
    target = [nebius_mk8s_v1_node_group.gpu]
  }

  assert {
    condition = (
      nebius_mk8s_v1_node_group.gpu[0].template.preemptible != null &&
      nebius_mk8s_v1_node_group.gpu[0].template.on_demand == null &&
      nebius_mk8s_v1_node_group.gpu[0].template.follows_spot_price != null &&
      nebius_mk8s_v1_node_group.gpu[0].template.spot_pricing_policy == null
    )
    error_message = "Preemptible GPU nodes without a pricing policy must follow the current spot price."
  }
}

run "preemptible_gpu_nodes_use_pricing_policy" {
  command = plan

  variables {
    gpu_nodes_preemptible            = true
    gpu_nodes_spot_pricing_policy_id = "pricingpolicy-gpu-test"
  }

  plan_options {
    target = [nebius_mk8s_v1_node_group.gpu]
  }

  assert {
    condition = (
      nebius_mk8s_v1_node_group.gpu[0].template.preemptible != null &&
      nebius_mk8s_v1_node_group.gpu[0].template.on_demand == null &&
      nebius_mk8s_v1_node_group.gpu[0].template.follows_spot_price == null &&
      nebius_mk8s_v1_node_group.gpu[0].template.spot_pricing_policy.id == "pricingpolicy-gpu-test"
    )
    error_message = "Preemptible node groups must use their configured pricing policy IDs."
  }
}

run "reject_pricing_policy_for_on_demand_gpu_nodes" {
  command = plan

  variables {
    gpu_nodes_spot_pricing_policy_id = "pricingpolicy-gpu-test"
  }

  plan_options {
    target = [nebius_mk8s_v1_node_group.gpu]
  }

  expect_failures = [nebius_mk8s_v1_node_group.gpu]
}
