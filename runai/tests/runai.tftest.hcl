run "runai_cluster_apply" {
  command = apply
  plan_options {
    target = [
      nebius_mk8s_v1_cluster.k8s-cluster
    ]
  }
}

run "runai_node_groups_apply" {
  command = apply
  plan_options {
    target = [
      nebius_iam_v1_service_account.k8s_node_group_sa,
      nebius_iam_v1_group_membership.k8s_node_group_sa-admin,
      nebius_mk8s_v1_node_group.cpu-only,
      nebius_mk8s_v1_node_group.gpu
    ]
  }
}

run "runai_ingress_apply" {
  command = apply
  plan_options {
    target = [
      helm_release.ingress_controller
    ]
  }
}

run "full_runai_apply" {
  command = apply

  assert {
    condition     = helm_release.runai_backend.status == "deployed"
    error_message = "runai-backend helm release failed to reach deployed status"
  }
}
