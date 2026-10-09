# Kubernetes Master parameters
variable "tenant_id" {
  description = "Tenant ID."
  type        = string
}

variable "parent_id" {
  description = "The ID of the folder that the Kubernetes cluster belongs to."
  type        = string
}

variable "cluster_id" {
  description = "K8s cluster id."
  type        = string
}

variable "namespace" {
  type    = string
  default = "o11y"
}


variable "o11y" {
  type = object({
    nebius_o11y_agent = optional(object({
      enabled                  = optional(bool, true)
      collectK8sClusterMetrics = optional(bool, false)
    })),
    grafana = optional(object({
      enabled       = optional(bool, true)
      pv_size       = optional(string, "25Gi")
      adminPassword = optional(string, "")
      nebius = optional(object({
        projectId   = optional(string, "")
        accessToken = optional(string, "")
      }), {})
    })),
    loki = optional(object({
      enabled            = optional(bool, true)
      region             = string
      replication_factor = optional(number, 2)
    })),
    prometheus = optional(object({
      enabled       = optional(bool, true)
      node_exporter = optional(bool, true)
      pv_size       = optional(string, "25Gi")
    }), {})
  })
  description = "Configuration of observability stack."
  default     = {}
}

variable "k8s_node_group_sa_id" {
  description = "ID of the existing k8s node group service account to use for Grafana"
  type        = string
  default     = null
}

variable "k8s_node_group_sa_enabled" {
  description = "Whether k8s node group service account is enabled"
  type        = bool
  default     = false
}

variable "test_mode" {
  description = "Switch between real usage and testing"
  type        = bool
  default     = false
}
