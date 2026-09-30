# Pure (provider-free) cluster-size dispatch for soperator.
# Derives an XS..XL tier from the worker node count and returns the per-component resource preset for that tier.

variable "worker_count" {
  description = "Total declared worker node count across all worker nodesets."
  type        = number
  nullable    = false

  validation {
    condition     = var.worker_count >= 0
    error_message = "worker_count must be >= 0."
  }
}

variable "sizing_tier_override" {
  description = "Force a specific tier (XS..XL); null auto-derives from worker_count."
  type        = string
  default     = null

  validation {
    condition     = var.sizing_tier_override == null ? true : contains(["XS", "S", "M", "L", "XL"], var.sizing_tier_override)
    error_message = "sizing_tier_override must be one of: XS, S, M, L, XL."
  }
}

variable "component_overrides" {
  description = <<-EOT
    Optional per-component resource overrides. Each entry replaces the component's effective value
    wholesale (the resolved tier's column, or the constant for non-tier-driven components) and uses
    exactly the same shape as the component_presets / constant_presets tables in main.tf.
    Components left unset keep their default values.
  EOT
  type = object({
    exporter                    = optional(object({ cpu = number, memory = number, ephemeral_storage = number }))
    rest                        = optional(object({ cpu = number, memory = number, ephemeral_storage = number }))
    mariadb                     = optional(object({ cpu = number, memory = number, ephemeral_storage = number }))
    node_configurator           = optional(object({ requests = object({ cpu = number, memory = number }), limits = object({ memory = number }) }))
    soperator_main_controller   = optional(object({ requests = object({ cpu = number, memory = number }), limits = object({ memory = number }) }))
    soperator_checks_controller = optional(object({ requests = object({ cpu = number, memory = number }), limits = object({ memory = number }) }))
    dcgm_exporter               = optional(object({ cpu = number, memory = number }))
    kruise_daemon               = optional(object({ cpu = number, memory = number }))
    nfs_server                  = optional(object({ cpu = number, memory = number }))
    kruise_manager              = optional(object({ cpu = string, memory = string }))
    kube_state_metrics          = optional(object({ requests = object({ cpu = string, memory = string }), limits = object({ memory = string }) }))
    vm_single                   = optional(object({ memory = string, cpu = string, size = string, gomaxprocs = number }))
    vm_agent                    = optional(object({ memory = string, cpu = string }))
    vm_logs                     = optional(object({ memory = string, cpu = string, size = string }))
    events_collector            = optional(object({ memory = string, cpu = string }))
    logs_collector              = optional(object({ memory = string, cpu = string }))
    jail_logs_collector         = optional(object({ memory = string, cpu = string }))
    nccl_profiles_collector     = optional(object({ memory = string, cpu = string }))
  })
  default  = {}
  nullable = false

  validation {
    condition = var.component_overrides.vm_single == null ? true : (
      can(regex("^\\d+Gi$", var.component_overrides.vm_single.size)) &&
      tonumber(trimsuffix(var.component_overrides.vm_single.size, "Gi")) % 93 == 0
    )
    error_message = "component_overrides.vm_single.size must be \"<N>Gi\" with N a multiple of 93 GiB (IO M3 disk size granularity)."
  }
}
