# Helm releases in four waves (clusters/common/apps `wave`): CRD-bearing charts first, then the platform
# controllers, then what depends on their webhooks. Values: clusters/common/values/<component>.yaml plus the
# per-cluster override computed in stage.tf.
locals {
  helm_values = { for n, a in local.helm_apps : n => concat(
    try(a.helmValues, "false") == "true" && fileexists("${local.repo}/clusters/common/values/${n}.yaml") ? [file("${local.repo}/clusters/common/values/${n}.yaml")] : [],
    [yamlencode(try(local.values_override[n], {}))]
  ) }
  # Wave overrides for Terraform's one-shot ordering (Argo CD retried until CRDs existed): the Prometheus
  # operator's CRDs (ServiceMonitor) must precede every chart that ships monitors (spegel, zot, opencost, ...).
  wave_override = { kube-prometheus-stack = "0", spegel = "1", zot = "2" }
  # Releases not waited for: envoy-gateway's rate-limit service needs the Redis of the wave-1 gateway manifests;
  # zot moves to wave 2: its StatefulSet mounts the cache claim charts/fleet renders after wave 1.
  no_wait   = ["envoy-gateway"]
  helm_wave = { for w in ["0", "1", "2", "3"] : w => { for n, a in local.helm_apps : n => a if lookup(local.wave_override, n, tostring(a.wave)) == w } }
}

resource "helm_release" "wave0" {
  for_each         = local.helm_wave["0"]
  name             = each.key
  namespace        = each.value.namespace
  create_namespace = true
  repository       = can(regex("^https?://", each.value.repoURL)) ? each.value.repoURL : "oci://${each.value.repoURL}"
  chart            = each.value.chart
  version          = each.value.version
  values           = local.helm_values[each.key]
  wait             = !contains(local.no_wait, each.key)
  timeout          = 900
  depends_on       = [kubernetes_namespace_v1.ns]
}

resource "helm_release" "wave1" {
  for_each         = local.helm_wave["1"]
  name             = each.key
  namespace        = each.value.namespace
  create_namespace = true
  repository       = can(regex("^https?://", each.value.repoURL)) ? each.value.repoURL : "oci://${each.value.repoURL}"
  chart            = each.value.chart
  version          = each.value.version
  values           = local.helm_values[each.key]
  wait             = !contains(local.no_wait, each.key)
  timeout          = 900
  depends_on       = [helm_release.wave0, kubectl_manifest.wave0, kubernetes_secret_v1.grafana_admin, kubernetes_secret_v1.zot_sync]
}

resource "helm_release" "wave2" {
  for_each         = local.helm_wave["2"]
  name             = each.key
  namespace        = each.value.namespace
  create_namespace = true
  repository       = can(regex("^https?://", each.value.repoURL)) ? each.value.repoURL : "oci://${each.value.repoURL}"
  chart            = each.value.chart
  version          = each.value.version
  values           = local.helm_values[each.key]
  wait             = true
  timeout          = 900
  depends_on       = [helm_release.wave1, helm_release.fleet, kubectl_manifest.wave1]
}

resource "helm_release" "wave3" {
  for_each         = local.helm_wave["3"]
  name             = each.key
  namespace        = each.value.namespace
  create_namespace = true
  repository       = can(regex("^https?://", each.value.repoURL)) ? each.value.repoURL : "oci://${each.value.repoURL}"
  chart            = each.value.chart
  version          = each.value.version
  values           = local.helm_values[each.key]
  wait             = true
  timeout          = 900
  depends_on       = [helm_release.wave2, kubectl_manifest.wave2, kubectl_manifest.postgres]
}

# charts/fleet: Kueue flavors and queues, MultiKueue manager objects, prices ConfigMap, image cache config
# and claim, pre-pull DaemonSets (worker-queues on workers, manager objects on the manager).
resource "helm_release" "fleet" {
  name             = "fleet"
  namespace        = "kueue-system"
  create_namespace = true
  chart            = "${local.repo}/charts/fleet"
  values           = [yamlencode(local.fleet_values)]
  wait             = false
  depends_on       = [helm_release.wave1, kubernetes_secret_v1.multikueue_remote]
}

# Optional add-on: Argo CD (operator UI only; nothing is deployed through it).
resource "helm_release" "argocd" {
  count            = local.f.argocd.enabled && local.role.control ? 1 : 0
  name             = "argocd"
  namespace        = "argocd"
  create_namespace = true
  repository       = "https://argoproj.github.io/argo-helm"
  chart            = "argo-cd"
  version          = "10.9.6"
  values           = [file("${local.repo}/clusters/control/apps/argocd-values.yaml")]
  depends_on       = [helm_release.wave1]
}
