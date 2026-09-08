variable "iam_project_id" {
  description = "ID of the IAM project."
  type        = string
}

variable "k8s_cluster_name" {
  description = "Name of the k8s cluster."
  type        = string
}

#---

variable "fs" {
  description = "WEKA filesystems."
  type = list(object({
    name            = string
    size_gibibytes  = number
    forbid_deletion = optional(bool, false)
  }))
}
