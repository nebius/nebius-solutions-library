variable "chart_version" {
  description = "Kueue Helm chart version."
  type        = string
  default     = "0.19.2"

  validation {
    condition     = can(regex("^[0-9]+\\.[0-9]+\\.[0-9]+$", var.chart_version))
    error_message = "chart_version must be a release version such as 0.19.2."
  }
}

variable "namespace" {
  description = "Kubernetes namespace for Kueue."
  type        = string
  default     = "kueue-system"

  validation {
    condition = (
      length(var.namespace) >= 1 &&
      length(var.namespace) <= 63 &&
      can(regex("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", var.namespace))
    )
    error_message = "namespace must be a valid Kubernetes namespace name."
  }
}

variable "timeout_seconds" {
  description = "Seconds to wait for the Helm release to become ready."
  type        = number
  default     = 300

  validation {
    condition     = var.timeout_seconds > 0
    error_message = "timeout_seconds must be greater than zero."
  }
}

variable "controller_node_selector" {
  description = "Node selector used to place the Kueue controller manager."
  type        = map(string)
  default     = {}
}

variable "topology_aware_scheduling" {
  description = "Explicitly enable Kueue topology-aware scheduling and create the topology resources."
  type        = bool
  default     = true
}

variable "topology_name" {
  description = "Name of the Kueue Topology resource."
  type        = string
  default     = "nebius-gpu-topology"

  validation {
    condition     = can(regex("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", var.topology_name))
    error_message = "topology_name must be a valid Kubernetes resource name."
  }
}

variable "topology_levels" {
  description = "Ordered node labels describing the GPU topology from broadest to narrowest."
  type        = list(string)
  default = [
    "topology.nebius.com/gpu-cluster-id",
    "topology.nebius.com/tier-2",
    "topology.nebius.com/tier-1",
    "kubernetes.io/hostname",
  ]

  validation {
    condition     = !var.topology_aware_scheduling || length(var.topology_levels) > 0
    error_message = "topology_levels must contain at least one node label when topology-aware scheduling is enabled."
  }
}

variable "resource_flavors" {
  description = "Kueue ResourceFlavors keyed by resource name. Each flavor selects one GPU node group."
  type = map(object({
    node_labels = map(string)
    tolerations = optional(list(map(string)), [])
  }))
  default = {}

  validation {
    condition = alltrue([
      for name, flavor in var.resource_flavors :
      can(regex("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", name)) && length(flavor.node_labels) > 0
    ])
    error_message = "Each resource flavor must have a valid Kubernetes name and at least one node label."
  }
}

variable "helm_values" {
  description = "Additional YAML values documents applied after the module defaults."
  type        = list(string)
  default     = []
}
