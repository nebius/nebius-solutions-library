output "sizing_tier" {
  description = "The resolved sizing tier (XS..XL)."
  value       = local.sizing_tier
}

output "preset" {
  description = "Effective per-component pod resources: the resolved tier's column with component_overrides applied."
  value       = local.preset
}

output "node_preset" {
  description = "Node VM preset per CPU nodeset for the resolved tier (controller/accounting/nfs/system -> preset string)."
  value       = local.node_preset
}

output "all_component_presets" {
  description = "The full component-major pod-resource table (component -> tier -> resources)."
  value       = local.component_presets
}

output "all_node_presets" {
  description = "The full component-major node-preset table (nodeset -> tier -> preset string)."
  value       = local.node_presets
}

output "kube_state_metrics_max_scrape_size" {
  description = "Cap (bytes) on the kube-state-metrics scrape response for the resolved tier; null keeps vmagent's global 32MiB guard."
  value       = local.kube_state_metrics_max_scrape_size_presets[local.sizing_tier]
}

output "capacity_violations" {
  description = <<-EOT
    Broken capacity invariants, one self-explaining message each (see the capacity locals in main.tf).
    Empty when every tier's component presets fit their nodes; the precondition makes any table edit
    that breaks an invariant a plan-time error.
  EOT
  value       = local.capacity_violations

  precondition {
    condition     = length(local.capacity_violations) == 0
    error_message = "Sizing-tier presets no longer fit their nodes. Fix the tables in modules/sizing_tier/main.tf:\n  - ${join("\n  - ", local.capacity_violations)}"
  }
}

output "system_nodes_needed" {
  description = <<-EOT
    Minimum system node count needed to hold the resolved tier's system-bound components
    (approximate; compare with the system nodeset's min_size).
  EOT
  value       = local.system_nodes_needed
}
