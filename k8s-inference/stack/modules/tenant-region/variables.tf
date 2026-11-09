variable "name" {
  description = "Name of the bucket, service account and group: <fleet>-<tenant>-<region>."
  type        = string
}

variable "tenant" {
  description = "Tenant name (terraform.tfvars `tenants.<name>`)."
  type        = string
}

variable "region" {
  description = "Nebius region of the bucket (its endpoint is storage.<region>.nebius.cloud)."
  type        = string
}

variable "project_id" {
  description = "Project of that region."
  type        = string
}

variable "lifecycle_days" {
  description = "Run outputs under operations/ expire after this many days; 0 keeps them."
  type        = number
}

variable "protect_data" {
  description = "Refuse `terraform destroy` of the bucket (prevent_destroy)."
  type        = bool
}

variable "labels" {
  description = "Labels on every resource."
  type        = map(string)
  default     = {}
}
