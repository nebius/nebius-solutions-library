# Everything the stages read from the fleet definition. `stack/config` is both a module (called by every
# stage with the raw `fleet` input) and a root `terraform console` evaluates over terraform.tfvars (stack.sh,
# tools/check.sh). The names match the locals in locals.tf one to one.

output "fleet" {
  description = "The fleet input with every default applied and every validation passed."
  value       = var.fleet
}

output "hub_region" {
  value = local.hub_region
}

output "hub_project" {
  value = local.hub_project
}

output "dedicated" {
  value = local.dedicated
}

output "region_clusters" {
  value = local.region_clusters
}

output "hub_id" {
  value = local.hub_id
}

output "control_cluster" {
  value = local.control_cluster
}

output "clusters" {
  value = local.clusters
}

output "control_id" {
  value = local.control_id
}

output "manager_id" {
  value = local.manager_id
}

output "cluster_ids" {
  value = local.cluster_ids
}

output "roles" {
  value = local.roles
}

output "projects" {
  value = local.projects
}

output "state_bucket" {
  value = local.state_bucket
}

output "state_region" {
  value = local.state_region
}

output "state_endpoint" {
  value = local.state_endpoint
}

output "images_host" {
  value = local.images_host
}

output "chart_prices" {
  value = local.chart_prices
}

output "chart_fleet" {
  value = local.chart_fleet
}

output "gpu_classes" {
  value = local.gpu_classes
}

output "plan" {
  value = local.plan
}

output "catalog_dir" {
  value = local.catalog_dir
}

output "bundled_catalog" {
  value = local.bundled_catalog
}

output "catalog_enabled" {
  value = local.catalog_enabled
}

output "catalog_raw" {
  value = local.catalog_raw
}

output "catalog" {
  value = local.catalog
}

output "catalog_without_fleet_class" {
  value = local.catalog_without_fleet_class
}

output "catalog_by_cluster" {
  value = local.catalog_by_cluster
}


output "prepull_by_cluster" {
  value = local.prepull_by_cluster
}
