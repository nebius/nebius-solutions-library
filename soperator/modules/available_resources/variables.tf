variable "cloud_environment" {
  description = "Cloud environment whose region/platform availability is used. Testing does not imply a non-production deployment in the production cloud."
  type        = string
  default     = "production"
  nullable    = false

  validation {
    condition     = contains(["production", "testing"], var.cloud_environment)
    error_message = "cloud_environment must be production or testing."
  }
}
