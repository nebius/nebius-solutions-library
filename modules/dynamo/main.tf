resource "nebius_applications_v1alpha1_k8s_release" "this" {
  parent_id        = var.parent_id
  cluster_id       = var.cluster_id
  application_name = var.config.release_name
  namespace        = var.config.namespace
  product_slug     = "nebius/nvidia-dynamo"

  sensitive = {
    set = {
      "platform.global.kai-scheduler.install"                 = var.config.scheduler_mode == "standalone"
      "platform.global.kai-scheduler.enabled"                 = true
      "platform.global.grove.install"                         = var.config.grove_mode == "bundled"
      "platform.global.grove.enabled"                         = true
      "platform.global.etcd.install"                          = false
      "platform.global.nats.install"                          = false
      "platform.global.snapshot.install"                      = false
      "platform.dynamo-operator.namespaceRestriction.enabled" = false
      "platform.dynamo-operator.upgradeCRD"                   = true
      "platform.dynamo-operator.discoveryBackend"             = "kubernetes"
    }
    values  = var.config.values
    version = sha256(jsonencode(var.config))
  }
}
