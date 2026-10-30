resource "helm_release" "this" {
  name             = var.config.release_name
  namespace        = var.config.namespace
  repository       = var.config.chart_path == null ? "https://helm.ngc.nvidia.com/nvidia/ai-dynamo" : null
  chart            = coalesce(var.config.chart_path, "dynamo-platform")
  version          = var.config.version
  create_namespace = true
  atomic           = true
  wait_for_jobs    = true
  timeout          = 1200

  values = concat([for filename in var.config.values_files : file(filename)], [yamlencode({
    global = {
      kai-scheduler = { install = var.config.scheduler_mode == "standalone", enabled = true }
      grove         = { install = var.config.grove_mode == "bundled", enabled = true }
      etcd          = { install = false }
      nats          = { install = false }
      snapshot      = { install = false }
    }
    dynamo-operator = { discoveryBackend = "kubernetes" }
    # Grove owns PodGroup termination; the bundled KAI chart requires this native setting.
    kai-scheduler = { scheduler = { args = { default-staleness-grace-period = "-1s" } } }
  })])
}
