module "k8s_rbac_bindings" {
  count = var.k8s_rbac_bindings.enabled ? 1 : 0

  source = "../modules/k8s-rbac-bindings"

  namespaces              = var.k8s_rbac_bindings.namespaces
  cluster_role_bindings   = var.k8s_rbac_bindings.cluster_role_bindings
  namespace_role_bindings = var.k8s_rbac_bindings.namespace_role_bindings
  default_labels = {
    "app.kubernetes.io/managed-by" = "terraform"
    "library-solution"             = "k8s-training"
  }

  providers = {
    kubernetes = kubernetes
  }

  depends_on = [
    nebius_mk8s_v1_node_group.cpu-only,
    nebius_mk8s_v1_node_group.gpu,
    nebius_mk8s_v1_node_group.gb300,
  ]
}
