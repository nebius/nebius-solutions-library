locals {
  gb300_dra_enabled = local.gb300_enabled && var.gb300.dra
}

# MK8s manages DRANet. This release supplies NVIDIA GPU DRA and ComputeDomains.
resource "helm_release" "nvidia_dra" {
  count = local.gb300_dra_enabled ? 1 : 0

  name             = "dra-driver-nvidia-gpu"
  repository       = "https://helm.ngc.nvidia.com/nvidia"
  chart            = "dra-driver-nvidia-gpu"
  version          = "0.4.1"
  namespace        = "nvidia-dra-driver-gpu"
  create_namespace = true
  wait             = true
  timeout          = 900

  values = [yamlencode({
    nvidiaDriverRoot            = "/"
    gpuResourcesEnabledOverride = true
    resourceApiVersion          = "resource.k8s.io/v1"

    resources = {
      gpus           = { enabled = true }
      computeDomains = { enabled = true }
    }

    kubeletPlugin = {
      nodeSelector = {
        "nvidia.com/dra-kubelet-plugin" = "true"
      }
      # Explicit GB300 selection replaces the chart's discovery-label affinity.
      affinity = null
    }
  })]

  depends_on = [
    nebius_mk8s_v1_node_group.gb300,
    module.network-operator,
  ]
}
