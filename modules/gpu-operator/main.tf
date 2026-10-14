resource "nebius_applications_v1alpha1_k8s_release" "this" {
  cluster_id = var.cluster_id
  parent_id  = var.parent_id

  application_name = "gpu-operator"
  namespace        = "gpu-operator"
  product_slug     = "nebius/nvidia-gpu-operator"

  sensitive = {
    set = {
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
}
