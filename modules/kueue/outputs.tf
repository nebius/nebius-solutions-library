output "release_name" {
  description = "Kueue Helm release name."
  value       = nonsensitive(helm_release.this.name)
}

output "namespace" {
  description = "Namespace containing the Kueue installation."
  value       = nonsensitive(helm_release.this.namespace)
}

output "chart_version" {
  description = "Installed Kueue Helm chart version."
  value       = nonsensitive(helm_release.this.version)
}

output "status" {
  description = "Kueue Helm release status."
  value       = nonsensitive(helm_release.this.status)
}

output "topology_aware_scheduling" {
  description = "Whether topology-aware scheduling resources are managed."
  value       = var.topology_aware_scheduling
}

output "topology_name" {
  description = "Managed Kueue Topology name. Null when topology-aware scheduling is disabled."
  value       = var.topology_aware_scheduling ? var.topology_name : null
}

output "resource_flavor_names" {
  description = "Managed Kueue ResourceFlavor names."
  value       = sort(keys(kubectl_manifest.resource_flavor))
}
