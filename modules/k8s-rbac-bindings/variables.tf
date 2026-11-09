variable "default_labels" {
  description = "Labels applied to all Kubernetes resources created by this module."
  type        = map(string)
  default     = {}
}

variable "namespaces" {
  description = "Optional namespaces to create before namespace-scoped RBAC bindings are applied."
  type = map(object({
    name        = optional(string)
    labels      = optional(map(string), {})
    annotations = optional(map(string), {})
  }))
  default = {}
}

variable "cluster_role_bindings" {
  description = "Cluster-wide RBAC bindings keyed by stable Terraform identity. Use for cluster read-only visibility or explicitly approved elevated access."
  type = map(object({
    name      = optional(string)
    role_name = string
    subjects = list(object({
      kind      = string
      name      = string
      api_group = optional(string)
      namespace = optional(string)
    }))
    labels      = optional(map(string), {})
    annotations = optional(map(string), {})
  }))
  default = {}

  validation {
    condition = alltrue([
      for _, binding in var.cluster_role_bindings :
      trimspace(binding.role_name) != "" && length(binding.subjects) > 0
    ])
    error_message = "Each cluster_role_bindings entry must set role_name and at least one subject."
  }

  validation {
    condition = alltrue(flatten([
      for _, binding in var.cluster_role_bindings : [
        for subject in binding.subjects :
        contains(["User", "Group", "ServiceAccount"], subject.kind) &&
        trimspace(subject.name) != "" &&
        (
          subject.kind != "ServiceAccount" ||
          try(trimspace(subject.namespace), "") != ""
        )
      ]
    ]))
    error_message = "Subjects must be User, Group, or ServiceAccount. ServiceAccount subjects must include namespace."
  }
}

variable "namespace_role_bindings" {
  description = "Namespace-scoped RBAC bindings keyed by stable Terraform identity. Use built-in ClusterRoles such as view, edit, or admin for target namespaces."
  type = map(object({
    name      = optional(string)
    namespace = string
    role_kind = optional(string, "ClusterRole")
    role_name = string
    subjects = list(object({
      kind      = string
      name      = string
      api_group = optional(string)
      namespace = optional(string)
    }))
    labels      = optional(map(string), {})
    annotations = optional(map(string), {})
  }))
  default = {}

  validation {
    condition = alltrue([
      for _, binding in var.namespace_role_bindings :
      trimspace(binding.namespace) != "" &&
      trimspace(binding.role_name) != "" &&
      contains(["Role", "ClusterRole"], binding.role_kind) &&
      length(binding.subjects) > 0
    ])
    error_message = "Each namespace_role_bindings entry must set namespace, role_name, a role_kind of Role or ClusterRole, and at least one subject."
  }

  validation {
    condition = alltrue(flatten([
      for _, binding in var.namespace_role_bindings : [
        for subject in binding.subjects :
        contains(["User", "Group", "ServiceAccount"], subject.kind) &&
        trimspace(subject.name) != "" &&
        (
          subject.kind != "ServiceAccount" ||
          try(trimspace(subject.namespace), "") != ""
        )
      ]
    ]))
    error_message = "Subjects must be User, Group, or ServiceAccount. ServiceAccount subjects must include namespace."
  }
}
