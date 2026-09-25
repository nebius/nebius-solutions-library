output "kubeflow_admin_password" {
  sensitive = true
  value     = random_password.kubeflow_admin.result
}

output "kubeflow_user_password" {
  sensitive = true
  value     = random_password.kubeflow_user.result
}
