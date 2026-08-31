variable "iam_project_id" {
  description = "ID of the IAM project."
  type        = string
}

variable "k8s_cluster_name" {
  description = "Name of the k8s cluster."
  type        = string
}

#---

variable "controller_spool" {
  description = "Filestore for Slurm controller's spool."
  type = object({
    existing = optional(object({
      id = string
    }))
    spec = optional(object({
      size_gibibytes       = number
      block_size_kibibytes = number
      forbid_deletion      = optional(bool, false)
    }))
  })
  nullable = false

  validation {
    condition = (
      var.controller_spool.existing != null && var.controller_spool.spec == null
    ) || (var.controller_spool.existing == null && var.controller_spool.spec != null)
    error_message = "One of `existing` or `spec` must be provided."
  }
}

variable "jail" {
  description = "Filesystem for Jail."
  type = object({
    existing = optional(object({
      id = string
    }))
    spec = optional(object({
      type                 = string
      size_gibibytes       = number
      block_size_kibibytes = number
      forbid_deletion      = optional(bool, false)
    }))
  })
  nullable = false

  validation {
    condition = (
      var.jail.existing != null && var.jail.spec == null
    ) || (var.jail.existing == null && var.jail.spec != null)
    error_message = "One of `existing` or `spec` must be provided."
  }

  validation {
    condition = (var.jail.spec == null
      ? true
      : contains(values(module.resources.shared_filesystem_types), var.jail.spec.type)
    )
    error_message = format(
      "Type should be one of [%s], got %s.",
      join(", ", values(module.resources.shared_filesystem_types)),
      coalesce(var.jail.spec.type, "none")
    )
  }
}

variable "jail_submounts" {
  description = "Filesystems for jail submounts."
  type = list(object({
    name = string
    existing = optional(object({
      id = string
    }))
    spec = optional(object({
      type                 = string
      size_gibibytes       = number
      block_size_kibibytes = number
      forbid_deletion      = optional(bool, false)
    }))
  }))
  default = []

  validation {
    condition = length([
      for sm in var.jail_submounts : true
      if(sm.existing != null && sm.spec == null) || (sm.existing == null && sm.spec != null)
    ]) == length(var.jail_submounts)
    error_message = "All submounts must have one of `existing` or `spec` provided."
  }

  validation {
    condition = alltrue([for sm in var.jail_submounts : (
      sm.spec == null
      ? true
      : contains(values(module.resources.shared_filesystem_types), sm.spec.type)
    )])
    error_message = format(
      "Type should be one of [%s].",
      join(", ", values(module.resources.shared_filesystem_types))
    )
  }
}

variable "accounting" {
  description = "Filestore for Slurm accounting database."
  type = object({
    existing = optional(object({
      id = string
    }))
    spec = optional(object({
      size_gibibytes       = number
      block_size_kibibytes = number
      forbid_deletion      = optional(bool, false)
    }))
  })
  nullable = true
  default  = null

  validation {
    condition = var.accounting != null ? (
      (var.accounting.existing != null && var.accounting.spec == null) ||
      (var.accounting.existing == null && var.accounting.spec != null)
    ) : true
    error_message = "One of `existing` or `spec` must be provided."
  }
}
