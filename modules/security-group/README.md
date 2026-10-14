# Nebius security group module

Creates a Nebius VPC security group and managed ingress/egress rules with validation around common misconfigurations: unrestricted ingress, invalid ports, invalid IPv4 CIDRs, empty security group IDs, mixed source/destination selectors, unsupported protocol/port combinations, and rule priority ranges.

Security groups apply implicit deny after rule evaluation. The module therefore expects callers to model every allowed path deliberately.

## Example

```hcl
module "bastion_security_group" {
  source = "../modules/security-group"

  parent_id  = var.parent_id
  network_id = data.nebius_vpc_v1_subnet.this.network_id
  name       = "bastion"

  ingress_rules = {
    ssh = {
      protocol          = "TCP"
      destination_ports = [22]
      source_cidrs      = ["203.0.113.10/32"]
      priority          = 100
    }
  }

  egress_rules = {
    internet = {
      protocol          = "ANY"
      destination_cidrs = ["0.0.0.0/0"]
      priority          = 900
    }
  }
}
```

Attach the group to an instance network interface:

```hcl
network_interfaces = [
  {
    name            = "eth0"
    subnet_id       = var.subnet_id
    ip_address      = {}
    security_groups = [module.bastion_security_group.network_interface_reference]
  }
]
```

## Notes

- `allow_unrestricted_ingress_rules` defaults to `false`. An `ALLOW` ingress rule with empty `source_cidrs` or `0.0.0.0/0` fails validation unless the caller explicitly opts in.
- `allow_unrestricted_egress_rules` defaults to `true` because most solutions need package repositories, Nebius APIs, container registries, or Object Storage. Set it to `false` for stricter workloads.
- The module creates only the egress rules passed in `egress_rules`. If `egress_rules = {}`, no egress rules are created, even when `allow_unrestricted_egress_rules = true`; that flag controls validation only.
- Use either CIDRs or a security group selector per rule, not both. Security group selector IDs must be non-empty when set.
- Ports are valid only on TCP and UDP rules. Do not set `destination_ports` for `ANY` or `ICMP` rules.
- Each rule supports up to 8 IPv4 CIDRs and up to 8 destination ports. Rule priorities must be between 1 and 1000.
- Keep rule keys stable. They are used by `for_each`, so renaming a key recreates that rule and may briefly remove the old rule before the replacement is ready.
- Cross-rule checks run during plan/apply via Terraform preconditions. `terraform validate` confirms syntax and provider schema, but it does not prove every rule passes those preconditions. If rule values are unknown during planning, Terraform may defer a precondition failure until apply.
