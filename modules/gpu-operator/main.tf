locals {
  helm_set = {
    "dcgmExporter.enabled"                                       = var.enable_dcgm_exporter
    "dcgmExporter.serviceMonitor.enabled"                        = var.enable_dcgm_service_monitor
    "dcgmExporter.serviceMonitor.honorLabels"                    = var.relabel_dcgm_exporter ? "false" : null
    "dcgmExporter.serviceMonitor.relabelings[0].action"          = var.relabel_dcgm_exporter ? "replace" : null
    "dcgmExporter.serviceMonitor.relabelings[0].regex"           = var.relabel_dcgm_exporter ? "nvidia-dcgm-exporter" : null
    "dcgmExporter.serviceMonitor.relabelings[0].replacement"     = var.relabel_dcgm_exporter ? "dcgm-exporter" : null
    "dcgmExporter.serviceMonitor.relabelings[0].sourceLabels[0]" = var.relabel_dcgm_exporter ? "__meta_kubernetes_pod_label_app" : null
    "dcgmExporter.serviceMonitor.relabelings[0].targetLabel"     = var.relabel_dcgm_exporter ? "app_kubernetes_io_name" : null
    "mig.strategy"                                               = var.mig_strategy != null ? var.mig_strategy : null
    "cdi.enabled"                                                = var.cdi_enabled
  }
}

resource "nebius_applications_v1alpha1_k8s_release" "this" {
  cluster_id = var.cluster_id
  parent_id  = var.parent_id

  application_name = "gpu-operator"
  namespace        = "gpu-operator"
  product_slug     = "nebius/nvidia-gpu-operator"

  sensitive = {
    # The nebius provider treats `sensitive` as write-only: changing `set`
    # alone does NOT trigger an update (provider schema: "Changing this field
    # will trigger an update of the resource even if no other fields are
    # changed" refers to `version`). Hash the rendered values so any change
    # to a helm value rolls the release instead of silently no-op'ing.
    # Verified against the nebius provider schema from 0.6.23 (the lowest
    # constraint of any caller of this module) upward: `sensitive.version`
    # exists on nebius_applications_v1alpha1_k8s_release throughout.
    # Existing deployments see a one-time in-place update of the k8s release
    # on the first apply after picking up this change.
    version = sha256(jsonencode(local.helm_set))
    set     = local.helm_set
  }
}
