variable "config" {
  description = "One native NIM Operator release; project NIMCache/NIMService resources and their credentials have separate owners."
  type = object({
    version      = optional(string, "3.1.2")
    namespace    = optional(string, "nim-operator")
    chart_path   = optional(string)
    values_files = optional(list(string), [])
    runai_cache_reader = optional(object({
      namespace       = optional(string, "runai")
      service_account = optional(string, "cluster-sync")
    }))
  })
  default = {}
}
