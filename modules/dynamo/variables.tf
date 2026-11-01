variable "cluster_id" {
  description = "K8s cluster id."
  type        = string
}

variable "parent_id" {
  description = "Project id."
  type        = string
}

variable "config" {
  description = "Marketplace Dynamo/Grove installation. standalone owns bundled KAI; runai/external reuse an existing compatible KAI owner."
  type = object({
    namespace      = optional(string, "dynamo-system")
    release_name   = optional(string, "dynamo-platform")
    scheduler_mode = optional(string, "standalone")
    grove_mode     = optional(string, "bundled")
    values         = optional(string)
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
