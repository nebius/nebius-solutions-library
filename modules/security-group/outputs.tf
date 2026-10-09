output "id" {
  description = "Security group ID."
  value       = nebius_vpc_v1_security_group.this.id
}

output "name" {
  description = "Security group name."
  value       = nebius_vpc_v1_security_group.this.name
}

output "network_id" {
  description = "VPC network ID."
  value       = nebius_vpc_v1_security_group.this.network_id
}

output "ingress_rule_ids" {
  description = "Ingress rule IDs keyed by input rule key."
  value       = { for key, rule in nebius_vpc_v1_security_rule.ingress : key => rule.id }
}

output "egress_rule_ids" {
  description = "Egress rule IDs keyed by input rule key."
  value       = { for key, rule in nebius_vpc_v1_security_rule.egress : key => rule.id }
}

output "network_interface_reference" {
  description = "Object shape expected by nebius_compute_v1_instance network_interfaces.security_groups."
  value = {
    id = nebius_vpc_v1_security_group.this.id
  }
}
