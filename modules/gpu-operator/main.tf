locals {
  toolkit_env = concat(
    var.toolkit_restart_mode == null ? [] : [{
      name  = "RUNTIME_RESTART_MODE"
      value = var.toolkit_restart_mode
    }],
    var.toolkit_config_source == null ? [] : [{
      name  = "RUNTIME_CONFIG_SOURCE"
      value = var.toolkit_config_source
    }],
  )

  helm_values = merge({
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
    "devicePlugin.enabled"                                       = var.device_plugin_enabled
    },
    { for index, item in local.toolkit_env : "toolkit.env[${index}].name" => item.name },
    { for index, item in local.toolkit_env : "toolkit.env[${index}].value" => item.value },
    { for index, item in var.driver_manager_env : "driver.manager.env[${index}].name" => item.name },
    { for index, item in var.driver_manager_env : "driver.manager.env[${index}].value" => item.value },
  )
}

resource "nebius_applications_v1alpha1_k8s_release" "this" {
  cluster_id = var.cluster_id
  parent_id  = var.parent_id

  application_name = "gpu-operator"
  namespace        = "gpu-operator"
  product_slug     = "nebius/nvidia-gpu-operator"

  sensitive = {
    set = local.helm_values
    # Write-only values change trigger; not an Operator/chart software pin.
    # The Marketplace release API does not expose a chart-version selector.
    version = sha256(jsonencode(local.helm_values))
  }
}
