# All providers are mocked; no cloud or Kubernetes API is contacted.
# Terraform mocks leave nested pool lists empty, so these plans check wiring
# and expect the worker CIDR precondition to fail. They cannot verify a
# successful configuration with populated worker CIDRs. The rejected node
# group also prevents its dependent LoadBalancer Service from being planned.
mock_provider "kubectl" {}
mock_provider "nebius" {
  override_during = plan

  mock_data "nebius_vpc_v1_subnet" {
    defaults = {
      network_id     = "vpcnetwork-e00r7z9vfxmg1bk99s"
      route_table_id = "vpcroutetable-e00r7z9vfxmg1bk99s"
    }
  }

  mock_resource "nebius_vpc_v1_subnet" {
    defaults = {
      id = "vpcsubnet-e00r7z9vfxmg1bk99t"
      status = {
        ipv4_public_cidrs = ["203.0.113.3/32", "203.0.113.1/32", "203.0.113.2/32"]
      }
    }
  }

  mock_resource "nebius_vpc_v1_allocation" {
    defaults = {
      id     = "vpcallocation-e00r7z9vfxmg1bk99s"
      status = { details = { allocated_cidr = "10.0.0.10/32" } }
    }
  }
}

override_resource {
  override_during = plan
  target          = nebius_vpc_v1_route_table.gateway_egress
  values          = { id = "vpcroutetable-e00r7z9vfxmg1bk99t" }
}

override_resource {
  override_during = plan
  target          = nebius_vpc_v1_route_table.workers
  values          = { id = "vpcroutetable-e00r7z9vfxmg1bk99v" }
}

variables {
  name              = "test-nat"
  project_id        = "project-e00r7z9vfxmg1bk99s"
  subnet_id         = "vpcsubnet-e00r7z9vfxmg1bk99s"
  cluster_subnet_id = "vpcsubnet-e00r7z9vfxmg1bk99c"
  mk8s_cluster_id   = "mk8scluster-e00r7z9vfxmg1bk99s"
  ssh_user_name     = "ubuntu"
  ssh_public_key    = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest nat-test"
}

run "network_and_policy_wiring" {
  command = plan

  module {
    source = "../modules/k8s-nat-gateway"
  }

  assert {
    condition = (
      nebius_vpc_v1_subnet.gateway.network_id == "vpcnetwork-e00r7z9vfxmg1bk99s" &&
      nebius_vpc_v1_route_table.workers.network_id == "vpcnetwork-e00r7z9vfxmg1bk99s" &&
      nebius_vpc_v1_route_table.gateway_egress.network_id == "vpcnetwork-e00r7z9vfxmg1bk99s" &&
      nebius_vpc_v1_subnet.gateway.id != var.subnet_id
    )
    error_message = "Gateways and worker routes must share the existing VPC, with gateways in a separate subnet."
  }

  assert {
    condition = (
      nebius_vpc_v1_subnet.gateway.route_table_id == nebius_vpc_v1_route_table.gateway_egress.id &&
      nebius_vpc_v1_route.gateway_egress.parent_id == nebius_vpc_v1_route_table.gateway_egress.id &&
      nebius_vpc_v1_route.gateway_egress.next_hop.default_egress_gateway == true &&
      nebius_vpc_v1_route.gateway_egress.destination.cidr == "0.0.0.0/0" &&
      nebius_vpc_v1_route_table.gateway_egress.id != nebius_vpc_v1_route_table.workers.id
    )
    error_message = "Gateway traffic must have a separate direct-egress path, never the worker route back to the LB."
  }

  assert {
    condition = (
      nebius_vpc_v1_route.workers.next_hop.allocation.id == nebius_vpc_v1_allocation.load_balancer.id &&
      nebius_vpc_v1_route.workers.parent_id == nebius_vpc_v1_route_table.workers.id &&
      nebius_vpc_v1_route.workers.destination.cidr == "0.0.0.0/0" &&
      nebius_vpc_v1_allocation.load_balancer.ipv4_private.subnet_id == var.cluster_subnet_id &&
      nebius_vpc_v1_allocation.load_balancer.ipv4_private.cidr == "/32" &&
      output.worker_route_table_id == nebius_vpc_v1_route_table.workers.id
    )
    error_message = "The prepared worker route must use the reserved private LB allocation and expose its route-table ID for cutover."
  }

  assert {
    condition = (
      nebius_vpc_v1_subnet.gateway.ipv4_public_pools.use_network_pools == false &&
      length(nebius_vpc_v1_subnet.gateway.ipv4_public_pools.pools[0].cidrs) == 3 &&
      output.public_ip_cidrs == tolist(["203.0.113.1/32", "203.0.113.2/32", "203.0.113.3/32"])
    )
    error_message = "The fixed public pool must cover both gateway nodes and the upgrade spare, with the complete public pool exported."
  }

  assert {
    condition = (
      yamldecode(kubectl_manifest.host_policy.yaml_body).spec.nodeSelector.matchLabels == local.node_selector &&
      yamldecode(kubectl_manifest.host_policy.yaml_body).spec.ingress == [{
        fromEntities = ["cluster", "kube-apiserver", "health"]
      }] &&
      yamldecode(kubectl_manifest.host_policy.yaml_body).spec.egress == [{
        toEntities = ["all"]
      }]
    )
    error_message = "The host policy must select gateway nodes, allow only cluster ingress without public SSH exceptions, and preserve outbound traffic."
  }

  expect_failures = [nebius_mk8s_v1_node_group.gateway]
}

run "reject_missing_surge_address" {
  command = plan

  module {
    source = "../modules/k8s-nat-gateway"
  }

  variables {
    node_count          = 3
    public_ip_pool_size = 3
  }

  expect_failures = [nebius_vpc_v1_subnet.gateway]
}
