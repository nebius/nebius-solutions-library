variable "iam_project_id" {
  description = "ID of the IAM project."
  type        = string
}

variable "instance_name" {
  description = "Cluster instance name to distinguish between multiple clusters in tenant."
  type        = string
}

variable "region" {
  description = "Region used to construct the Object Storage endpoint for existing buckets."
  type        = string
}

variable "bucket" {
  description = "Nebius Object Storage bucket for training checkpoints. Provide `existing` to reuse a bucket (e.g. to resume training from another cluster's checkpoints), or `spec` to create one. Set `existing.project_id` for a bucket in another project of the same tenant and `existing.endpoint` for a bucket in another region. Cross-tenant bucket reuse is not supported."
  type = object({
    existing = optional(object({
      name       = string
      endpoint   = optional(string)
      project_id = optional(string)
    }))
    spec = optional(object({
      name = optional(string)
    }))
  })
  nullable = false

  validation {
    condition = (
      (var.bucket.existing != null && var.bucket.spec == null) ||
      (var.bucket.existing == null && var.bucket.spec != null)
    )
    error_message = "One of `existing` or `spec` must be provided."
  }

  validation {
    condition = (
      try(var.bucket.existing.project_id, null) == null ||
      can(regex("^project-[a-z0-9]+$", try(var.bucket.existing.project_id, "")))
    )
    error_message = "`existing.project_id` must be a Nebius project ID such as `project-...`."
  }

  validation {
    condition = (
      try(var.bucket.existing.endpoint, null) == null ||
      can(regex("^https://storage\\.[a-z0-9-]+\\.nebius\\.cloud(:443)?/?$", try(var.bucket.existing.endpoint, "")))
    )
    error_message = "`existing.endpoint` must be a Nebius Object Storage endpoint such as `https://storage.eu-north1.nebius.cloud:443`."
  }
}
