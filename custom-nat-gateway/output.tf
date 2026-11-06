output "network_id" {
  description = "ID of the existing VPC network used by the NAT gateway solution."
  value       = var.vpc_network_id
}

output "gateway_public_ip" {
  description = "Public IP address of the gateway VM."
  value       = split("/", nebius_vpc_v1_allocation.gateway_public_ip.status.details.allocated_cidr)[0]
}

output "gateway_private_ip" {
  description = "Private IP address used as the route next hop."
  value       = split("/", nebius_vpc_v1_allocation.gateway_private_ip.status.details.allocated_cidr)[0]
}

output "workload_private_ip" {
  description = "Private IP address of the workload VM when deploy_test_vm is true."
  value       = var.deploy_test_vm ? split("/", nebius_vpc_v1_allocation.workload_private_ip[0].status.details.allocated_cidr)[0] : null
}

output "route_table_id" {
  description = "ID of the custom route table assigned to the private subnet."
  value       = nebius_vpc_v1_route_table.nat_gateway_table.id
}

output "workload_subnet_id" {
  description = "ID of the private workload subnet."
  value       = nebius_vpc_v1_subnet.private_subnet.id
}

output "ssh_jump_command" {
  description = "SSH command that jumps through the gateway VM to the private workload VM when deploy_test_vm is true."
  value       = var.deploy_test_vm ? "ssh -J ${var.ssh_user_name}@${split("/", nebius_vpc_v1_allocation.gateway_public_ip.status.details.allocated_cidr)[0]} ${var.ssh_user_name}@${split("/", nebius_vpc_v1_allocation.workload_private_ip[0].status.details.allocated_cidr)[0]}" : null
}
