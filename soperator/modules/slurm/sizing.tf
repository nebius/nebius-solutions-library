# Cluster-size dispatch. Resolves the XS..XL tier from the worker count (or
# the forced var.sizing_tier_override) and exposes the per-component resource preset
# consumed in locals.tf.
module "sizing" {
  source = "../sizing_tier"

  worker_count         = local.worker_count
  sizing_tier_override = var.sizing_tier_override
  component_overrides  = var.component_overrides
}
