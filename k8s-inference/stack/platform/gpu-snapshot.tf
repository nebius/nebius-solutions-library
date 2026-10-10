# An optional, separately owned release. This does not change the serving scaler,
# Kueue admission or any pod without a snapshot profile. CPU storage access keeps
# snapshot metadata available when the region's GPU pools scale to zero.
locals {
  snapshot_config  = local.clusters[var.target].gpu_snapshot
  snapshot_enabled = local.role.worker && local.snapshot_config.enabled
  snapshot_defaults = {
    sharedFilesystem = {
      enabled      = true
      hostPath     = "/mnt/weights/gpu-snapshot"
      capacity     = "${local.clusters[var.target].weights_filesystem.size_gib}Gi"
      nodeSelector = { "serverless2.nebius/snapshot-store" = "true" }
    }
    snapshot = {
      image = {
        agent    = { repository = "${local.images_host}/ghcr/ai-dynamo/snapshot/agent" }
        operator = { repository = "${local.images_host}/ghcr/ai-dynamo/snapshot/operator" }
      }
      daemonset = { nodeSelector = { "serverless2.nebius/snapshot-store" = "true" } }
      operator  = { nodeSelector = { "serverless2.nebius/pool" = "system", "serverless2.nebius/snapshot-store" = "true" } }
    }
  }
}

resource "kubernetes_namespace_v1" "gpu_snapshot" {
  count = local.snapshot_enabled ? 1 : 0
  metadata {
    name = local.snapshot_config.namespace
    labels = {
      "app.kubernetes.io/part-of"          = "gpu-snapshot"
      "pod-security.kubernetes.io/enforce" = "privileged"
      "pod-security.kubernetes.io/audit"   = "privileged"
      "pod-security.kubernetes.io/warn"    = "privileged"
    }
  }
  lifecycle {
    precondition {
      condition     = startswith(local.snapshot_config.namespace, "gpu-snapshot-")
      error_message = "GPU Snapshot needs its own gpu-snapshot-* namespace."
    }
    precondition {
      condition     = local.clusters[var.target].weights_filesystem.enabled && local.clusters[var.target].weights_filesystem.mount_on_system
      error_message = "The fleet Snapshot integration needs the shared filesystem mounted on system nodes. Enable weights_filesystem.mount_on_system first."
    }
    precondition {
      condition     = can(yamldecode(local.snapshot_config.values_yaml))
      error_message = "gpu_snapshot.values_yaml must be valid YAML."
    }
    precondition {
      condition     = local.snapshot_config.chart != ""
      error_message = "gpu_snapshot.chart must name a reviewed packaged chart or a repository chart."
    }
  }
}

resource "helm_release" "gpu_snapshot" {
  count      = local.snapshot_enabled ? 1 : 0
  name       = "gpu-snapshot"
  namespace  = kubernetes_namespace_v1.gpu_snapshot[0].metadata[0].name
  chart      = local.snapshot_config.chart
  repository = local.snapshot_config.repository
  version    = local.snapshot_config.version
  values = [yamlencode(local.snapshot_defaults), local.snapshot_config.values_yaml,
  yamlencode({ namespace = { create = false } })]
  wait       = true
  timeout    = 900
  depends_on = [helm_release.wave3]
}
