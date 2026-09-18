output "monitoring" {
  description = "Monitoring metadata."
  value = var.telemetry_enabled ? {
    namespace = local.namespace
    } : {
    namespace = null
  }
}

output "debug_resources" {
  description = "resources"

  value = local.resources
}

output "sizing_tier" {
  description = "Sizing tier (XS..XL) derived from the worker count or forced via var.sizing_tier_override."
  value       = local.sizing_tier
}

output "debug_size_resources" {
  description = "Effective size-driven component resources (tier column with component_overrides merged), for tests/inspection."
  value = merge(local.selected_preset, {
    sizing_tier  = local.sizing_tier
    worker_count = local.worker_count
  })
}
