module "k8s_rbac_bindings" {
  source = "../.."

  namespaces = {
    workload = {
      name = "workload"
    }
  }

  namespace_role_bindings = {
    workload_admin = {
      name      = "workload-admin"
      namespace = "workload"
      role_kind = "ClusterRole"
      role_name = "admin"
      subjects = [
        {
          kind      = "Group"
          name      = "nebius:viewer"
          api_group = "rbac.authorization.k8s.io"
        }
      ]
    }
  }
}
