output "bastion_host_public_ip" {
  value = trimsuffix(nebius_compute_v1_instance.bastion_instance.status.network_interfaces[0].public_ip_address.address, "/32")
}
output "bastion_service_account" {
  value = nebius_iam_v1_service_account.bastion-sa.id
}

output "bastion_security_group_id" {
  description = "Managed bastion security group ID. Null when enable_bastion_security_group is false."
  value       = var.enable_bastion_security_group ? module.bastion_security_group[0].id : null
}

output "bastion_security_group_rules" {
  description = "Managed bastion security group rule IDs. Null when enable_bastion_security_group is false."
  value = var.enable_bastion_security_group ? {
    ingress = module.bastion_security_group[0].ingress_rule_ids
    egress  = module.bastion_security_group[0].egress_rule_ids
  } : null
}

output "bastion_security_group_access_note" {
  description = "Access note for the managed bastion security group."
  value = var.enable_bastion_security_group && length(local.bastion_ssh_source_cidrs) == 0 ? (
    "No default SSH ingress rule was created from bastion_allowed_ssh_cidrs. Set bastion_allowed_ssh_cidrs, bastion_extra_ingress_rules, bastion_extra_security_group_ids, or disable the managed security group if SSH access is required."
  ) : null
}
