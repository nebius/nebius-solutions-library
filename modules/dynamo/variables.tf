variable "config" {
  description = "Native Dynamo/Grove installation. standalone owns bundled KAI; runai/external reuse an existing compatible KAI owner."
  type = object({
    version        = optional(string, "1.5.1")
    chart_path     = optional(string)
    namespace      = optional(string, "dynamo-system")
    release_name   = optional(string, "dynamo-platform")
    scheduler_mode = optional(string, "standalone")
    grove_mode     = optional(string, "bundled")
    values_files   = optional(list(string), [])
  })
  default = {}

  validation {
    condition     = contains(["standalone", "runai", "external"], var.config.scheduler_mode)
    error_message = "scheduler_mode must be standalone, runai or external."
  }

  validation {
    condition     = contains(["bundled", "external"], var.config.grove_mode)
    error_message = "grove_mode must be bundled or external."
  }
}
