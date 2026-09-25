run "custom_nat_gateway_apply" {
  command = apply

  assert {
    condition     = nebius_vpc_v1_subnet.private_subnet.route_table_id == nebius_vpc_v1_route_table.nat_gateway_table.id
    error_message = "Private subnet is not associated with the custom NAT gateway route table."
  }

  assert {
    condition     = nebius_vpc_v1_route.default_to_gateway.next_hop.allocation.id == nebius_vpc_v1_allocation.gateway_private_ip.id
    error_message = "Default route does not point to the gateway private allocation."
  }

  assert {
    condition     = output.gateway_public_ip != ""
    error_message = "Gateway public IP output is empty."
  }

  assert {
    condition     = output.gateway_private_ip != ""
    error_message = "Gateway private IP output is empty."
  }

  assert {
    condition     = output.workload_private_ip != ""
    error_message = "Workload private IP output is empty."
  }
}
