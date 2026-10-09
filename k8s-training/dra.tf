resource "helm_release" "nvidia_dra_driver" {
  count = var.gpu_dra.enabled ? 1 : 0

  name             = "dra-driver-nvidia-gpu"
  repository       = var.gpu_dra.chart_repository
  chart            = "dra-driver-nvidia-gpu"
  version          = var.gpu_dra.chart_version
  namespace        = var.gpu_dra.namespace
  create_namespace = true
  atomic           = true
  cleanup_on_fail  = true
  wait             = true
  timeout          = 600

  values = [yamlencode({
    nvidiaDriverRoot            = "/run/nvidia/driver"
    gpuResourcesEnabledOverride = true
    resources = {
      gpus = {
        enabled = true
      }
      computeDomains = {
        enabled = false
      }
    }
    featureGates = {
      # MIG Manager owns the static geometry. Enabling DynamicMIG here would
      # create a second owner that may tear down those MIG devices.
      DynamicMIG = false
    }
    kubeletPlugin = {
      nodeSelector = merge(
        {
          "nvidia.com/dra-kubelet-plugin" = "true"
        },
        local.managed_mig_dra_enabled ? {
          # Do not enumerate GPUs while MIG Manager is still converging. This
          # applies to both partitioned profiles and all-disabled transitions.
          # MIG Manager removes/changes this state during reconfiguration, which
          # also makes the DaemonSet recycle its per-node plugin pod.
          "nvidia.com/mig.config.state" = "success"
        } : {},
      )
    }
  })]

  depends_on = [
    kubernetes_job_v1.mig_config_reconciler,
    module.gpu-operator,
    nebius_mk8s_v1_node_group.gpu,
  ]
}
