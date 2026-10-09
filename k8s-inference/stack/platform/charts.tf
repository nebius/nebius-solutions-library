# Helm releases in four waves (clusters/common/apps `wave`): CRD-bearing charts first, then the platform
# controllers, then what depends on their webhooks. Values: clusters/common/values/<component>.yaml plus the
# per-cluster override computed in stage.tf.
locals {
  helm_values = { for n, a in local.helm_apps : n => concat(
    try(a.helmValues, "false") == "true" && fileexists("${local.repo}/clusters/common/values/${n}.yaml") ? [file("${local.repo}/clusters/common/values/${n}.yaml")] : [],
    [yamlencode(try(local.values_override[n], {}))]
  ) }
  # Wave overrides for CRD readiness: the Prometheus
  # operator's CRDs (ServiceMonitor) must precede every chart that ships monitors (spegel, zot, opencost, ...).
  wave_override = { kube-prometheus-stack = "0", spegel = "1", zot = "2" }
  # Releases not waited for: envoy-gateway's rate-limit service needs the Redis of the wave-1 gateway manifests;
  # zot moves to wave 2: its StatefulSet mounts the cache claim charts/fleet renders after wave 1. kueue: Helm's
  # uninstall --wait never finishes on its aggregated ClusterRoles (the kube-controller-manager re-creates them
  # while Helm deletes the roles that feed them); readiness is checked by terraform_data.kueue_ready instead and
  # the stale roles are removed at destroy time (stack/scripts/kueue-uninstall-cleanup.sh).
  no_wait   = ["envoy-gateway", "kueue"]
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

# Runs after the wave-1 Kueue release is gone (wave1 depends on it, so destroy orders it last): removes the
# aggregated ClusterRoles the controller re-created during the uninstall.
resource "terraform_data" "kueue_uninstall_cleanup" {
  count = contains(keys(local.helm_wave["1"]), "kueue") ? 1 : 0
  # Only the cluster's address and CA are inputs (and therefore in the state); the IAM token is read from
  # NEBIUS_IAM_TOKEN of the calling shell at run time (stack.sh exports it; stack/scripts/kube.sh).
  input = { script = "${path.module}/../scripts/kueue-uninstall-cleanup.sh", server = local.cluster.endpoint, ca = local.cluster.cluster_ca_certificate }
  provisioner "local-exec" {
    when        = destroy
    command     = self.input.script
    environment = { KUBE_SERVER = self.input.server, KUBE_CA = self.input.ca }
  }
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
  depends_on       = [helm_release.wave0, kubectl_manifest.wave0, kubernetes_secret_v1.grafana_admin, kubernetes_secret_v1.zot_sync, kubernetes_secret_v1.cost_export_s3, terraform_data.kueue_uninstall_cleanup]
}

# Kueue is installed without Helm's wait (see no_wait); wave 2 and the fleet chart need its webhook, so this
# waits for the controller rollout the way Helm would have.
resource "terraform_data" "kueue_ready" {
  count            = contains(keys(local.helm_wave["1"]), "kueue") ? 1 : 0
  triggers_replace = [helm_release.wave1["kueue"].version, helm_release.wave1["kueue"].metadata]
  provisioner "local-exec" {
    command     = "${path.module}/../scripts/kube.sh -n ${local.helm_wave["1"]["kueue"].namespace} rollout status deployment/kueue-controller-manager --timeout=900s"
    environment = { KUBE_SERVER = local.cluster.endpoint, KUBE_CA = local.cluster.cluster_ca_certificate } # token: NEBIUS_IAM_TOKEN of the shell
  }
  depends_on = [helm_release.wave1]
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
  depends_on       = [helm_release.wave1, terraform_data.kueue_ready, helm_release.fleet, kubectl_manifest.wave1]
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
  depends_on       = [helm_release.wave2, kubectl_manifest.wave2, kubernetes_job_v1.database_init, kubernetes_secret_v1.litellm_db, kubernetes_config_map_v1.trust]
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
  depends_on       = [helm_release.wave1, terraform_data.kueue_ready, kubernetes_secret_v1.multikueue_remote]
}
