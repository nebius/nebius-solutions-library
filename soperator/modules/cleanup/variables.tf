variable "iam_project_id" {
  description = "ID of the IAM project."
  type        = string
}

variable "expected_cluster_id" {
  description = "Expected CSI owner of explicitly approved disks. Supply a literal ID, not a reference to the cluster that depends on this module."
  type        = string
  default     = ""
  nullable    = false

  validation {
    condition     = var.expected_cluster_id == "" || startswith(var.expected_cluster_id, "mk8scluster-")
    error_message = "expected_cluster_id must be empty or a Managed Kubernetes cluster ID."
  }
}

variable "disk_ids" {
  description = "Exact disk IDs explicitly approved for deletion on destroy. Empty by default to preserve leftover and retained disks."
  type        = set(string)
  default     = []
  nullable    = false

  validation {
    condition     = alltrue([for id in var.disk_ids : startswith(id, "computedisk-") && length(id) > length("computedisk-")])
    error_message = "disk_ids must contain only Compute disk IDs."
  }

  validation {
    condition     = length(var.disk_ids) == 0 || length(var.expected_cluster_id) > length("mk8scluster-")
    error_message = "An explicit expected_cluster_id is required when disk_ids is nonempty."
  }
}
