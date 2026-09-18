output "regions" {
  description = "Supported regions."
  value       = [for k, v in local.regions : v]
}

output "platforms" {
  description = "Supported platforms."
  value       = [for k, v in local.platforms : v]
}

output "presets" {
  description = "Supported presets."
  value       = [for k, v in local.presets : v]
}

output "platform_regions" {
  description = "Map of supported regions grouped by platform."
  value       = local.platform_regions
}

output "cpu_platform_by_platform" {
  description = "Map of binary-compatible CPU platform grouped by Nebius platform."
  value       = local.cpu_platform_by_platform
}

output "by_platform" {
  description = "Map of available resource presets grouped by platform."
  value       = local.presets_by_platforms
}

output "local_nvme_supported_by_region_platform_preset" {
  description = "Local NVMe support matrix by region/platform/preset."
  value       = local.local_nvme_supported_by_region_platform_preset
}

output "k8s_ephemeral_storage_coefficient" {
  value = local.reserve.ephemeral_storage.coefficient
}

output "k8s_ephemeral_storage_reserve" {
  value = local.reserve.ephemeral_storage.count
}

output "disk_types" {
  description = "Supported disk types."
  value       = local.disk_types
}

output "filesystem_types" {
  description = "Supported filesystem types."
  value       = local.filesystem_types
}

output "shared_filesystem_types" {
  description = "Supported shared filesystem types."
  value       = local.shared_filesystem_types
}

output "cpu_topology_by_platform" {
  description = "CPU topologies preset by platform."
  value       = local.cpu_topologies_by_platforms
}

output "gres_name_by_platform" {
  description = "GRes name by platform."
  value       = local.gres_by_platforms
}

output "gres_config_by_platform" {
  description = "GRes config by platform."
  value       = local.gres_config_by_platforms
}
