output "namespace" {
  description = "Namespace where the External Secrets Operator is installed."
  value       = var.namespace
}

output "helm_release_name" {
  description = "Name of the External Secrets Operator Helm release."
  value       = helm_release.this.name
}

output "chart_version" {
  description = "Pinned Helm chart version for the External Secrets Operator release."
  value       = helm_release.this.version
}
