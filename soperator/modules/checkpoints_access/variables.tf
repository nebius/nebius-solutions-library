variable "iam_group_parent_id" {
  description = "ID of the project that owns the checkpoint bucket, or the IAM tenant for cross-project bucket access."
  type        = string
}

variable "iam_project_id" {
  description = "ID of the IAM project."
  type        = string
}

variable "instance_name" {
  description = "Cluster instance name to distinguish between multiple clusters in tenant."
  type        = string
}

variable "k8s_cluster_context" {
  description = "K8s context name for kubectl."
  type        = string
}

variable "k8s_cluster_id" {
  description = "ID of the K8s cluster, used to set up kubectl context on destroy."
  type        = string
}

variable "soperator_namespace" {
  description = "Kubernetes namespace in which to create the checkpoint Object Storage credentials secret."
  type        = string
}

variable "region" {
  description = "Region of the Object Storage bucket."
  type        = string
}

variable "bucket_name" {
  description = "Name of the checkpoints bucket."
  type        = string
}

variable "bucket_id" {
  description = "ID of the checkpoints bucket to grant the workload service account access to."
  type        = string
}

variable "bucket_endpoint" {
  description = "Endpoint URL of the checkpoints bucket."
  type        = string
}

variable "create_k8s_secret" {
  description = "Whether to create the Object Storage credentials secret in the k8s cluster. Disable to manage credential delivery yourself."
  type        = bool
  default     = true
}

variable "jail_env_file_owner" {
  description = "Numeric `uid:gid` owner of /etc/nebius-checkpoints.env in the jail. Numeric because the renderer container does not share the jail's /etc/passwd. Set to the uid:gid (or 0:<shared gid>) of the users who submit training jobs so they can source the file."
  type        = string
  default     = "0:0"

  validation {
    condition     = can(regex("^[0-9]+:[0-9]+$", var.jail_env_file_owner))
    error_message = "Must be a numeric uid:gid pair, e.g. \"0:0\" or \"1000:1000\"."
  }
}

variable "jail_env_file_mode" {
  description = "File mode of /etc/nebius-checkpoints.env in the jail. Use 640 with a group owner to grant a user group read access."
  type        = string
  default     = "600"

  validation {
    condition     = can(regex("^0?[0-7]{3}$", var.jail_env_file_mode))
    error_message = "Must be a three-digit octal mode with an optional leading zero, e.g. \"600\", \"640\", or \"0640\"."
  }
}
