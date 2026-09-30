locals {
  namespaces = {
    for key, namespace in var.namespaces : key => merge(namespace, {
      name = try(trimspace(namespace.name), "") != "" ? trimspace(namespace.name) : key
    })
  }

  cluster_role_bindings = {
    for key, binding in var.cluster_role_bindings : key => merge(binding, {
      name = try(trimspace(binding.name), "") != "" ? trimspace(binding.name) : key
      subjects = [
        for subject in binding.subjects : merge(subject, {
          kind      = trimspace(subject.kind)
          name      = trimspace(subject.name)
          api_group = try(trimspace(subject.api_group), "") != "" ? trimspace(subject.api_group) : (subject.kind == "ServiceAccount" ? null : "rbac.authorization.k8s.io")
          namespace = try(trimspace(subject.namespace), "") != "" ? trimspace(subject.namespace) : (subject.kind == "ServiceAccount" ? null : "")
        })
      ]
    })
  }

  namespace_role_bindings = {
    for key, binding in var.namespace_role_bindings : key => merge(binding, {
      name      = try(trimspace(binding.name), "") != "" ? trimspace(binding.name) : key
      namespace = trimspace(binding.namespace)
      role_kind = trimspace(binding.role_kind)
      role_name = trimspace(binding.role_name)
      subjects = [
        for subject in binding.subjects : merge(subject, {
          kind      = trimspace(subject.kind)
          name      = trimspace(subject.name)
          api_group = try(trimspace(subject.api_group), "") != "" ? trimspace(subject.api_group) : (subject.kind == "ServiceAccount" ? null : "rbac.authorization.k8s.io")
          namespace = try(trimspace(subject.namespace), "") != "" ? trimspace(subject.namespace) : (subject.kind == "ServiceAccount" ? null : "")
        })
      ]
    })
  }
}

resource "kubernetes_namespace_v1" "this" {
  for_each = local.namespaces

  metadata {
    name        = each.value.name
    labels      = merge(var.default_labels, each.value.labels)
    annotations = each.value.annotations
  }
}

resource "kubernetes_cluster_role_binding_v1" "this" {
  for_each = local.cluster_role_bindings

  metadata {
    name        = each.value.name
    labels      = merge(var.default_labels, each.value.labels)
    annotations = each.value.annotations
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = each.value.role_name
  }

  dynamic "subject" {
    for_each = each.value.subjects

    content {
      kind      = subject.value.kind
      name      = subject.value.name
      api_group = subject.value.api_group
      namespace = try(subject.value.namespace, null)
    }
  }
}

resource "kubernetes_role_binding_v1" "this" {
  for_each = local.namespace_role_bindings

  metadata {
    name        = each.value.name
    namespace   = each.value.namespace
    labels      = merge(var.default_labels, each.value.labels)
    annotations = each.value.annotations
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = each.value.role_kind
    name      = each.value.role_name
  }

  dynamic "subject" {
    for_each = each.value.subjects

    content {
      kind      = subject.value.kind
      name      = subject.value.name
      api_group = subject.value.api_group
      namespace = try(subject.value.namespace, null)
    }
  }

  depends_on = [
    kubernetes_namespace_v1.this,
  ]
}
