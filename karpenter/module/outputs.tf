output "mk8s-cluster-id" {
  value = nebius_mk8s_v1_cluster.cluster.id
}

output "iam-service-account-id" {
  value = nebius_iam_v1_service_account.node-sa.id
}
