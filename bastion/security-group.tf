data "nebius_vpc_v1_subnet" "bastion" {
  count = var.enable_bastion_security_group ? 1 : 0

  id = var.subnet_id
}

module "bastion_security_group" {
  count = var.enable_bastion_security_group ? 1 : 0

  source = "../modules/security-group"

  parent_id  = var.parent_id
  network_id = data.nebius_vpc_v1_subnet.bastion[0].network_id
  name       = var.bastion_security_group_name

  labels = {
    "library-solution" = "bastion"
  }

  allow_unrestricted_ingress_rules = var.bastion_allow_unrestricted_ingress_rules
  ingress_rules                    = merge(local.bastion_default_ingress_rules, var.bastion_extra_ingress_rules)
  egress_rules                     = merge(local.bastion_default_egress_rules, var.bastion_extra_egress_rules)
}

check "bastion_default_ssh_ingress" {
  assert {
    condition = (
      !var.enable_bastion_security_group ||
      length(local.bastion_ssh_source_cidrs) > 0
    )
    error_message = "No default SSH ingress rule was created from bastion_allowed_ssh_cidrs. Set bastion_allowed_ssh_cidrs, bastion_extra_ingress_rules, bastion_extra_security_group_ids, or disable the managed security group if SSH access is required."
  }
}
