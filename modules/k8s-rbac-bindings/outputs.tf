output "namespaces" {
  description = "Namespaces created by this module."
  value = {
    for key, namespace in kubernetes_namespace_v1.this : key => {
      id   = namespace.id
      name = namespace.metadata[0].name
    }
  }
}

output "cluster_role_bindings" {
  description = "ClusterRoleBindings created by this module."
  value = {
    for key, binding in kubernetes_cluster_role_binding_v1.this : key => {
      id        = binding.id
      name      = binding.metadata[0].name
      role_name = binding.role_ref[0].name
    }
  }
}

output "namespace_role_bindings" {
  description = "RoleBindings created by this module."
  value = {
    for key, binding in kubernetes_role_binding_v1.this : key => {
      id        = binding.id
      name      = binding.metadata[0].name
      namespace = binding.metadata[0].namespace
      role_kind = binding.role_ref[0].kind
      role_name = binding.role_ref[0].name
    }
  }
}
