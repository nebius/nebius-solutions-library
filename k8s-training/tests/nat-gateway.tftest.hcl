# All providers are mocked. Simulated applies exercise Terraform wiring only;
# no cloud or Kubernetes API is contacted.
mock_provider "kubectl" {}
mock_provider "nebius" {
  mock_data "nebius_vpc_v1_subnet" {
    defaults = {
      network_id     = "vpcnetwork-e00r7z9vfxmg1bk99s"
      route_table_id = "vpcroutetable-e00r7z9vfxmg1bk99s"
      status = {
        # Terraform mocks generate empty nested pool lists. Supply the
        # equivalent flat CIDRs to exercise the compatibility fallback.
        ipv4_private_cidrs = ["10.72.0.0/16", "10.70.0.0/15"]
      }
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
  target = nebius_vpc_v1_route_table.gateway_egress
  values = { id = "vpcroutetable-e00r7z9vfxmg1bk99t" }
}

override_resource {
  target = nebius_vpc_v1_route_table.workers
  values = { id = "vpcroutetable-e00r7z9vfxmg1bk99v" }
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

run "normal_configuration" {
  command = apply

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
      nebius_mk8s_v1_node_group.gateway.parent_id == var.mk8s_cluster_id &&
      nebius_mk8s_v1_node_group.gateway.template.network_interfaces[0].subnet_id == nebius_vpc_v1_subnet.gateway.id &&
      nebius_mk8s_v1_node_group.gateway.template.network_interfaces[0].public_ip_address != null &&
      nebius_mk8s_v1_node_group.gateway.template.metadata.labels["nebius.com/nat-gateway"] == "true" &&
      nebius_mk8s_v1_node_group.gateway.template.metadata.labels["nebius.com/exclude-from-implicit-load-balancer-selection"] == "true" &&
      !contains(keys(nebius_mk8s_v1_node_group.gateway.template.metadata.labels), "node.kubernetes.io/exclude-from-external-load-balancers") &&
      nebius_mk8s_v1_node_group.gateway.template.taints[0].effect == "NO_SCHEDULE"
    )
    error_message = "Only the dedicated, labelled and tainted gateway node group should consume the gateway subnet's public IPs."
  }

  assert {
    condition = (
      nebius_vpc_v1_subnet.gateway.ipv4_public_pools.use_network_pools == false &&
      length(nebius_vpc_v1_subnet.gateway.ipv4_public_pools.pools[0].cidrs) == 3 &&
      nebius_mk8s_v1_node_group.gateway.fixed_node_count == 2 &&
      nebius_mk8s_v1_node_group.gateway.strategy.max_surge.count == 1 &&
      nebius_mk8s_v1_node_group.gateway.strategy.max_unavailable.count == 0 &&
      output.public_ip_cidrs == tolist(["203.0.113.1/32", "203.0.113.2/32", "203.0.113.3/32"])
    )
    error_message = "The fixed public pool must cover both gateway nodes and the upgrade spare, with the complete public pool exported."
  }

  assert {
    condition = (
      yamldecode(kubectl_manifest.service.yaml_body).metadata.annotations["nebius.com/load-balancer-allocation-id"] == nebius_vpc_v1_route.workers.next_hop.allocation.id &&
      yamldecode(kubectl_manifest.service.yaml_body).metadata.annotations["nebius.com/load-balancer-type"] == "internal" &&
      yamldecode(kubectl_manifest.service.yaml_body).metadata.annotations["nebius.com/load-balancer-target-node-selector"] == "nebius.com/nat-gateway=true" &&
      yamldecode(kubectl_manifest.service.yaml_body).spec.type == "LoadBalancer" &&
      yamldecode(kubectl_manifest.service.yaml_body).spec.externalTrafficPolicy == "Cluster" &&
      yamldecode(kubectl_manifest.service.yaml_body).metadata.namespace == yamldecode(kubectl_manifest.namespace.yaml_body).metadata.name &&
      !contains(keys(yamldecode(kubectl_manifest.service.yaml_body).spec), "selector")
    )
    error_message = "The internal Cluster-policy Service must bind the route's allocation and explicitly select gateway nodes without Pod endpoints."
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

  assert {
    condition = (
      strcontains(one(yamldecode(nebius_mk8s_v1_node_group.gateway.template.cloud_init_user_data).bootcmd), "ExecStartPre=/usr/local/lib/nebius-nat-gateway/setup.sh") &&
      one([for file in yamldecode(nebius_mk8s_v1_node_group.gateway.template.cloud_init_user_data).write_files : file.content if file.path == "/etc/default/nebius-nat-gateway"]) == "WORKER_CIDRS='10.70.0.0/15 10.72.0.0/16'\n" &&
      alltrue([for name in ["setup.sh", "verify.sh"] : one([
        for file in yamldecode(nebius_mk8s_v1_node_group.gateway.template.cloud_init_user_data).write_files :
        file.permissions == "0755" && startswith(file.content, "#!/bin/sh\n")
        if file.path == "/usr/local/lib/nebius-nat-gateway/${name}"
      ])])
    )
    error_message = "Gateway user-data must install the early kubelet hook, host scripts and every worker source CIDR."
  }
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

run "legacy_subnet_cidrs" {
  command = plan

  module {
    source = "../modules/k8s-nat-gateway"
  }

  override_data {
    target = data.nebius_vpc_v1_subnet.workers
    values = {
      network_id = "vpcnetwork-e00r7z9vfxmg1bk99s"
      status = {
        ipv4_private_cidrs = ["10.80.0.0/16"]
      }
    }
  }

  assert {
    condition     = local.worker_cidrs == tolist(["10.80.0.0/16"])
    error_message = "Older subnet responses must still supply the NAT source CIDRs."
  }
}
