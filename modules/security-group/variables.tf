variable "parent_id" {
  description = "Project ID where the security group will be created."
  type        = string
}

variable "network_id" {
  description = "VPC network ID where this security group applies."
  type        = string
}

variable "name" {
  description = "Security group name."
  type        = string

  validation {
    condition     = length(trimspace(var.name)) > 0
    error_message = "name must not be empty."
  }
}

variable "labels" {
  description = "Labels to apply to the security group and all rules unless overridden per rule."
  type        = map(string)
  default     = {}
}

variable "allow_unrestricted_ingress_rules" {
  description = "Allow ingress rules that match any source, such as empty source_cidrs or 0.0.0.0/0."
  type        = bool
  default     = false
}

variable "allow_unrestricted_egress_rules" {
  description = "Allow egress rules that match any destination, such as empty destination_cidrs or 0.0.0.0/0."
  type        = bool
  default     = true
}

variable "ingress_rules" {
  description = "Ingress security rules keyed by stable rule names."
  type = map(object({
    name                     = optional(string)
    access                   = optional(string, "ALLOW")
    protocol                 = optional(string, "TCP")
    rule_type                = optional(string, "STATEFUL")
    priority                 = optional(number, 500)
    source_cidrs             = optional(list(string), [])
    source_security_group_id = optional(string)
    destination_ports        = optional(list(number), [])
    labels                   = optional(map(string), {})
  }))
  default = {}

  validation {
    condition = alltrue([
      for _, rule in var.ingress_rules : contains(["ALLOW", "DENY"], rule.access)
    ])
    error_message = "Ingress rule access must be ALLOW or DENY."
  }

  validation {
    condition = alltrue([
      for _, rule in var.ingress_rules : contains(["ANY", "TCP", "UDP", "ICMP"], rule.protocol)
    ])
    error_message = "Ingress rule protocol must be ANY, TCP, UDP, or ICMP."
  }

  validation {
    condition = alltrue([
      for _, rule in var.ingress_rules : contains(["STATEFUL", "STATELESS"], rule.rule_type)
    ])
    error_message = "Ingress rule type must be STATEFUL or STATELESS."
  }

  validation {
    condition = alltrue([
      for _, rule in var.ingress_rules : rule.priority >= 1 && rule.priority <= 1000
    ])
    error_message = "Ingress rule priority must be between 1 and 1000."
  }

  validation {
    condition = alltrue(flatten([
      for _, rule in var.ingress_rules : [
        for port in rule.destination_ports : port >= 1 && port <= 65535
      ]
    ]))
    error_message = "Ingress destination ports must be between 1 and 65535."
  }

  validation {
    condition = alltrue([
      for _, rule in var.ingress_rules : length(rule.destination_ports) <= 8
    ])
    error_message = "Each ingress rule can include at most 8 destination ports."
  }

  validation {
    condition = alltrue([
      for _, rule in var.ingress_rules : length(rule.source_cidrs) <= 8
    ])
    error_message = "Each ingress rule can include at most 8 source CIDRs."
  }

  validation {
    condition = alltrue([
      for _, rule in var.ingress_rules : rule.source_security_group_id == null || length(rule.source_cidrs) == 0
    ])
    error_message = "Ingress rules cannot set both source_cidrs and source_security_group_id."
  }

  validation {
    condition = alltrue([
      for _, rule in var.ingress_rules : try(length(trimspace(rule.source_security_group_id)) > 0, true)
    ])
    error_message = "Ingress source_security_group_id must not be empty when set."
  }

  validation {
    condition = alltrue([
      for _, rule in var.ingress_rules : contains(["TCP", "UDP"], rule.protocol) || length(rule.destination_ports) == 0
    ])
    error_message = "Ingress destination_ports can only be set for TCP or UDP rules."
  }

  validation {
    condition = alltrue(flatten([
      for _, rule in var.ingress_rules : [
        for cidr in rule.source_cidrs :
        can(cidrhost(cidr, 0)) && can(regex("^((25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\\.){3}(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])/([0-9]|[12][0-9]|3[0-2])$", cidr))
      ]
    ]))
    error_message = "Ingress source_cidrs must contain valid IPv4 CIDR blocks."
  }
}

variable "egress_rules" {
  description = "Egress security rules keyed by stable rule names."
  type = map(object({
    name                          = optional(string)
    access                        = optional(string, "ALLOW")
    protocol                      = optional(string, "ANY")
    rule_type                     = optional(string, "STATEFUL")
    priority                      = optional(number, 900)
    destination_cidrs             = optional(list(string), [])
    destination_security_group_id = optional(string)
    destination_ports             = optional(list(number), [])
    labels                        = optional(map(string), {})
  }))
  default = {}

  validation {
    condition = alltrue([
      for _, rule in var.egress_rules : contains(["ALLOW", "DENY"], rule.access)
    ])
    error_message = "Egress rule access must be ALLOW or DENY."
  }

  validation {
    condition = alltrue([
      for _, rule in var.egress_rules : contains(["ANY", "TCP", "UDP", "ICMP"], rule.protocol)
    ])
    error_message = "Egress rule protocol must be ANY, TCP, UDP, or ICMP."
  }

  validation {
    condition = alltrue([
      for _, rule in var.egress_rules : contains(["STATEFUL", "STATELESS"], rule.rule_type)
    ])
    error_message = "Egress rule type must be STATEFUL or STATELESS."
  }

  validation {
    condition = alltrue([
      for _, rule in var.egress_rules : rule.priority >= 1 && rule.priority <= 1000
    ])
    error_message = "Egress rule priority must be between 1 and 1000."
  }

  validation {
    condition = alltrue(flatten([
      for _, rule in var.egress_rules : [
        for port in rule.destination_ports : port >= 1 && port <= 65535
      ]
    ]))
    error_message = "Egress destination ports must be between 1 and 65535."
  }

  validation {
    condition = alltrue([
      for _, rule in var.egress_rules : length(rule.destination_ports) <= 8
    ])
    error_message = "Each egress rule can include at most 8 destination ports."
  }

  validation {
    condition = alltrue([
      for _, rule in var.egress_rules : length(rule.destination_cidrs) <= 8
    ])
    error_message = "Each egress rule can include at most 8 destination CIDRs."
  }

  validation {
    condition = alltrue([
      for _, rule in var.egress_rules : rule.destination_security_group_id == null || length(rule.destination_cidrs) == 0
    ])
    error_message = "Egress rules cannot set both destination_cidrs and destination_security_group_id."
  }

  validation {
    condition = alltrue([
      for _, rule in var.egress_rules : try(length(trimspace(rule.destination_security_group_id)) > 0, true)
    ])
    error_message = "Egress destination_security_group_id must not be empty when set."
  }

  validation {
    condition = alltrue([
      for _, rule in var.egress_rules : contains(["TCP", "UDP"], rule.protocol) || length(rule.destination_ports) == 0
    ])
    error_message = "Egress destination_ports can only be set for TCP or UDP rules."
  }

  validation {
    condition = alltrue(flatten([
      for _, rule in var.egress_rules : [
        for cidr in rule.destination_cidrs :
        can(cidrhost(cidr, 0)) && can(regex("^((25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\\.){3}(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])/([0-9]|[12][0-9]|3[0-2])$", cidr))
      ]
    ]))
    error_message = "Egress destination_cidrs must contain valid IPv4 CIDR blocks."
  }
}
