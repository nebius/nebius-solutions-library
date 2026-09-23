output "namespace" {
  description = "Namespace where the ExternalSecret and target Secret are managed."
  value       = var.namespace
}

output "secret_store_name" {
  description = "Name of the SecretStore or ClusterSecretStore referenced by the ExternalSecret."
  value       = var.secret_store_name
}

output "secret_store_kind" {
  description = "Kind of store referenced by the ExternalSecret."
  value       = var.secret_store_kind
}

output "external_secret_name" {
  description = "Name of the ExternalSecret resource."
  value = (
    var.external_secret_name != null && trimspace(var.external_secret_name) != ""
    ? trimspace(var.external_secret_name)
    : var.target_secret_name
  )
}

output "secret_name" {
  description = "Name of the target Kubernetes Secret populated from MysteryBox."
  value       = var.target_secret_name
}

output "mysterybox_secret_id" {
  description = "Nebius MysteryBox secret ID that backs the target Kubernetes Secret."
  value       = var.mysterybox_secret_id
}
