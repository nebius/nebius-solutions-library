resource "terraform_data" "validate_rules" {
  input = {
    ingress_rules = var.ingress_rules
    egress_rules  = var.egress_rules
  }

  lifecycle {
    precondition {
      condition = var.allow_unrestricted_ingress_rules || alltrue([
        for _, rule in var.ingress_rules :
        rule.access != "ALLOW" ||
        (
          rule.source_security_group_id != null ||
          (
            length(rule.source_cidrs) > 0 &&
            !contains(rule.source_cidrs, "0.0.0.0/0") &&
            !contains(rule.source_cidrs, "::/0") &&
            !contains(rule.source_cidrs, "0::/0")
          )
        )
      ])
      error_message = "ALLOW ingress rules must specify restricted source_cidrs or source_security_group_id unless allow_unrestricted_ingress_rules is true."
    }

    precondition {
      condition = var.allow_unrestricted_egress_rules || alltrue([
        for _, rule in var.egress_rules :
        rule.access != "ALLOW" ||
        (
          rule.destination_security_group_id != null ||
          (
            length(rule.destination_cidrs) > 0 &&
            !contains(rule.destination_cidrs, "0.0.0.0/0") &&
            !contains(rule.destination_cidrs, "::/0") &&
            !contains(rule.destination_cidrs, "0::/0")
          )
        )
      ])
      error_message = "ALLOW egress rules must specify restricted destination_cidrs or destination_security_group_id unless allow_unrestricted_egress_rules is true."
    }
  }
}

resource "nebius_vpc_v1_security_group" "this" {
  parent_id  = var.parent_id
  network_id = var.network_id
  name       = var.name
  labels     = var.labels

  depends_on = [
    terraform_data.validate_rules
  ]
}

resource "nebius_vpc_v1_security_rule" "ingress" {
  for_each = var.ingress_rules

  parent_id = nebius_vpc_v1_security_group.this.id
  name      = coalesce(each.value.name, "${var.name}-${each.key}")
  labels    = merge(var.labels, each.value.labels)

  access   = each.value.access
  protocol = each.value.protocol
  type     = each.value.rule_type
  priority = each.value.priority

  ingress = {
    source_cidrs             = each.value.source_cidrs
    source_security_group_id = each.value.source_security_group_id
    destination_ports        = each.value.destination_ports
  }
}

resource "nebius_vpc_v1_security_rule" "egress" {
  for_each = var.egress_rules

  parent_id = nebius_vpc_v1_security_group.this.id
  name      = coalesce(each.value.name, "${var.name}-${each.key}")
  labels    = merge(var.labels, each.value.labels)

  access   = each.value.access
  protocol = each.value.protocol
  type     = each.value.rule_type
  priority = each.value.priority

  egress = {
    destination_cidrs             = each.value.destination_cidrs
    destination_security_group_id = each.value.destination_security_group_id
    destination_ports             = each.value.destination_ports
  }
}
