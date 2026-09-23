locals {
  ssh_public_key = var.ssh_public_key.key != null ? var.ssh_public_key.key : (
  fileexists(var.ssh_public_key.path) ? file(var.ssh_public_key.path) : null)

  bastion_ssh_source_cidrs          = var.bastion_allowed_ssh_cidrs == null ? [] : var.bastion_allowed_ssh_cidrs
  bastion_wireguard_source_cidrs    = var.bastion_allowed_wireguard_cidrs == null ? local.bastion_ssh_source_cidrs : var.bastion_allowed_wireguard_cidrs
  bastion_wireguard_ui_source_cidrs = var.bastion_allowed_wireguard_ui_cidrs == null ? [] : var.bastion_allowed_wireguard_ui_cidrs

  bastion_default_ingress_rules = merge(
    length(local.bastion_ssh_source_cidrs) > 0 ? {
      ssh = {
        protocol          = "TCP"
        destination_ports = [22]
        source_cidrs      = local.bastion_ssh_source_cidrs
        priority          = 100
      }
    } : {},
    length(local.bastion_wireguard_source_cidrs) > 0 ? {
      wireguard = {
        protocol          = "UDP"
        destination_ports = [51820]
        source_cidrs      = local.bastion_wireguard_source_cidrs
        priority          = 110
      }
    } : {},
    length(local.bastion_wireguard_ui_source_cidrs) > 0 ? {
      wireguard_ui = {
        protocol          = "TCP"
        destination_ports = [5000]
        source_cidrs      = local.bastion_wireguard_ui_source_cidrs
        priority          = 120
      }
    } : {}
  )

  bastion_default_egress_rules = length(var.bastion_egress_cidrs) > 0 ? {
    internet = {
      protocol          = "ANY"
      destination_cidrs = var.bastion_egress_cidrs
      priority          = 900
    }
  } : {}

  bastion_managed_security_group_ids = var.enable_bastion_security_group ? [module.bastion_security_group[0].id] : []
  bastion_security_group_ids         = concat(local.bastion_managed_security_group_ids, var.bastion_extra_security_group_ids)
  bastion_security_group_refs        = length(local.bastion_security_group_ids) > 0 ? [for id in local.bastion_security_group_ids : { id = id }] : null
}
