resource "nebius_applications_v1alpha1_k8s_release" "this" {
  parent_id        = var.parent_id
  cluster_id       = var.cluster_id
  application_name = var.config.release_name
  namespace        = var.config.namespace
  product_slug     = "nebius/nvidia-dynamo"

  sensitive = {
    set = {
      "global.kai-scheduler.install"                 = var.config.scheduler_mode == "standalone"
      "global.kai-scheduler.enabled"                 = true
      "global.grove.install"                         = var.config.grove_mode == "bundled"
      "global.grove.enabled"                         = true
      "global.etcd.install"                          = false
      "nats.enabled"                                 = false
      "dynamo-operator.nats.enabled"                 = false
      "dynamo-operator.namespaceRestriction.enabled" = false
      "dynamo-operator.upgradeCRD"                   = true
      "dynamo-operator.discoveryBackend"             = "kubernetes"
    }
    values  = var.config.values
    version = sha256(jsonencode(var.config))
  }
}
