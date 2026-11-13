variable "config" {
  description = "Pinned upstream MPI Operator manifest; optional regional mirror keeps the vendor objects unchanged."
  type = object({
    version       = optional(string, "0.8.2")
    manifest_file = optional(string)
  })
  default = {}
}
