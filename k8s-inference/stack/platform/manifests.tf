# The platform's own manifests: charts/platform (one component per template directory) rendered by Helm with
# this cluster's values (stack/config/locals.tf `platform_values`: hostnames, images, roles, feature flags) and
# applied server-side, one kubectl_manifest per object in the component's wave (clusters/common/apps `wave`),
# next to the upstream Helm releases. Never a Helm release: Helm would want its ownership labels on every object,
# while server-side apply through the kubectl provider owns them as they are. The resources are keyed
# <group or _>/<Kind>/<namespace or _>/<name>, so every object keeps a stable state address across releases;
# tools/check.sh holds the render equal to the record in stack/platform/tests/golden.
data "helm_template" "platform" {
  name         = "platform"
  chart        = "${local.repo}/charts/platform"
  kube_version = "${local.f.kubernetes_version}.0"
  values       = [yamlencode(local.platform_values)]
}

locals {
  # One entry per rendered object: the component (the template directory, from Helm's `# Source:` line) and the object.
  platform_rendered = [for d in split("\n---\n", "\n${data.helm_template.platform.manifest}") : {
    component = regex("# Source: platform/templates/([^/\n]+)/", d)[0]
    object    = yamldecode(d)
  } if length(regexall("(?m)^[^#\n]", d)) > 0] # a chunk of comments only (a template's header before its first ---) is no object
  platform_objects = { for r in local.platform_rendered :
    "${length(split("/", r.object.apiVersion)) > 1 ? split("/", r.object.apiVersion)[0] : "_"}/${r.object.kind}/${try(r.object.metadata.namespace, "_")}/${r.object.metadata.name}" => r
  }
  # The `models` certificate (charts/platform/templates/gateway/models-tls.yaml): its hostnames and issuer
  # follow the models defined through the API, which rewrites them; Terraform only creates the placeholder.
  models_certificate        = "Certificate[/|]envoy-gateway-system[/|]models$"
  models_certificate_fields = ["spec.dnsNames", "spec.issuerRef", "spec.commonName"]
  manifests_wave = { for w in ["0", "1", "2", "3"] : w => {
    for k, r in local.platform_objects : k => jsonencode(r.object) if tostring(try(local.manifest_apps[r.component].wave, "none")) == w
  } }
}

resource "kubectl_manifest" "wave0" {
  for_each          = local.manifests_wave["0"]
  yaml_body         = each.value
  server_side_apply = true
  force_conflicts   = true
  ignore_fields     = can(regex(local.models_certificate, each.key)) ? local.models_certificate_fields : null
  wait              = false
  depends_on        = [kubernetes_namespace_v1.ns]
}

resource "kubectl_manifest" "wave1" {
  for_each          = local.manifests_wave["1"]
  yaml_body         = each.value
  server_side_apply = true
  force_conflicts   = true
  ignore_fields     = can(regex(local.models_certificate, each.key)) ? local.models_certificate_fields : null
  wait              = false
  depends_on        = [helm_release.wave0, kubectl_manifest.wave0, helm_release.wave1]
}

resource "kubectl_manifest" "wave2" {
  for_each          = local.manifests_wave["2"]
  yaml_body         = each.value
  server_side_apply = true
  force_conflicts   = true
  ignore_fields     = can(regex(local.models_certificate, each.key)) ? local.models_certificate_fields : null
  wait              = false
  depends_on        = [helm_release.wave2, kubectl_manifest.wave1, helm_release.fleet]
}

resource "kubectl_manifest" "wave3" {
  for_each          = local.manifests_wave["3"]
  yaml_body         = each.value
  server_side_apply = true
  force_conflicts   = true
  ignore_fields     = can(regex(local.models_certificate, each.key)) ? local.models_certificate_fields : null
  wait              = false
  depends_on        = [helm_release.wave3, kubectl_manifest.wave2, kubernetes_config_map_v1.catalog]
}
# ---------------------------------------------------------------------------
# Extras rendered from this stage's inputs (what the per-cluster overlays added as files).
locals {
  extras_wave1 = merge(
    # TLS certificate for the public hostnames of this cluster (HTTP-01 through the gateway). The model
    # endpoints' hostnames are on the `models` certificate the API keeps (gateway/models-tls.yaml).
    {
      certificate = {
        apiVersion = "cert-manager.io/v1", kind = "Certificate"
        metadata   = { name = "${local.id}-wildcard", namespace = "envoy-gateway-system" }
        spec = {
          secretName = "${local.id}-wildcard-tls"
          issuerRef  = { name = local.certificate_issuer, kind = "ClusterIssuer" }
          commonName = try(local.hostnames.api, local.hostnames.grafana) # a GPU region has no API hostname
          dnsNames   = distinct(concat(values(local.hostnames), local.role.control ? local.f.edge.extra_hostnames : []))
          privateKey = { rotationPolicy = "Always" }
        }
      }
    },
    { for k, v in {
      fleet_ca_issuer = {
        apiVersion = "cert-manager.io/v1", kind = "ClusterIssuer"
        metadata   = { name = "fleet-ca" }
        spec       = { ca = { secretName = "fleet-ca" } }
      }
    } : k => v if local.fleet_ca_here },
    { for k, v in {
      private_issuer = {
        apiVersion = "cert-manager.io/v1", kind = "ClusterIssuer"
        metadata   = { name = local.certificate_issuer }
        spec       = { ca = { secretName = local.f.edge.private_ca_secret } }
      }
    } : k => v if local.f.edge.mode == "internal" },
    # Let's Encrypt IP-address certificate (ACME profile shortlived) for https://<ip>, when asked for.
    { for k, v in {
      ip_certificate = {
        apiVersion = "cert-manager.io/v1", kind = "Certificate"
        metadata   = { name = "${local.id}-ip", namespace = "envoy-gateway-system" }
        spec = {
          secretName  = "${local.id}-ip-tls"
          issuerRef   = { name = local.f.edge.acme.staging ? "letsencrypt-staging" : "letsencrypt", kind = "ClusterIssuer" }
          ipAddresses = [local.cluster.gateway_ip]
          duration    = "144h"
          renewBefore = "48h"
          privateKey  = { rotationPolicy = "Always" }
        }
      }
    } : k => v if local.f.edge.ip_certificate && local.cluster.gateway_ip != null },
    # Source allow-list on the public HTTPS listener (the HTTP listener stays open for ACME).
    { for k, v in {
      source_cidrs = {
        apiVersion = "gateway.envoyproxy.io/v1alpha1", kind = "SecurityPolicy"
        metadata   = { name = "source-cidrs", namespace = "envoy-gateway-system" }
        spec = {
          targetRefs    = [{ group = "gateway.networking.k8s.io", kind = "Gateway", name = "serverless2-external", sectionName = "https" }]
          authorization = { defaultAction = "Deny", rules = [{ name = "allowed-sources", action = "Allow", principal = { clientCIDRs = local.f.edge.source_cidrs } }] }
        }
      }
    } : k => v if length(local.f.edge.source_cidrs) > 0 },
    # Shared weights filesystem as a static RWX volume + claim (models/weights-shared).
    { for k, v in {
      weights_pv = {
        apiVersion = "v1", kind = "PersistentVolume"
        metadata   = { name = "weights-shared", labels = { "serverless2.nebius/weights" = "shared" } }
        spec = {
          capacity                      = { storage = "${local.cluster.weights_filesystem.size_gib}Gi" }, accessModes = ["ReadWriteMany"]
          persistentVolumeReclaimPolicy = "Retain", storageClassName = "weights-shared", volumeMode = "Filesystem"
          hostPath                      = { path = "/mnt/weights", type = "Directory" }, claimRef = { namespace = "models", name = "weights-shared" }
        }
      }
    } : k => v if local.cluster.weights_filesystem.enabled },
  )
  # The claim on the static volume, kept apart from the other extras: a claim bound to a static PV cannot be
  # resized ("only dynamically provisioned pvc can be resized"), so growing `weights_filesystem.size_gib`
  # day-2 must change the PV's capacity only. The claim is written once (ignore_changes), sized as the
  # volume was when the claim was created; `kubectl get pvc` then shows that first size, the mount has the real one.
  weights_pvc = local.cluster.weights_filesystem.enabled ? {
    weights_pvc = {
      apiVersion = "v1", kind = "PersistentVolumeClaim"
      metadata   = { name = "weights-shared", namespace = "models" }
      spec = {
        accessModes = ["ReadWriteMany"], storageClassName = "weights-shared", volumeName = "weights-shared"
        resources   = { requests = { storage = "${local.cluster.weights_filesystem.size_gib}Gi" } }
      }
    }
  } : {}

  # Control plane only: the LiteLLM route and the billing CronJob.
  extras_control = { for k, v in {
    litellm_route = {
      apiVersion = "gateway.networking.k8s.io/v1", kind = "HTTPRoute", metadata = { name = "ui-litellm", namespace = "litellm" }
      spec = {
        parentRefs = [{ name = "serverless2-external", namespace = "envoy-gateway-system", sectionName = "https" }]
        # a chat call through LiteLLM waits for the endpoint's cold start like the API route does (charts/platform/templates/api);
        # Envoy's default of 15 s answered "504 upstream request timeout" on a scaled-to-zero 72B model
        hostnames = [try(local.hostnames.litellm, "")]
        rules     = [{ backendRefs = [{ name = "litellm", port = 4000 }], timeouts = { request = "630s", backendRequest = "630s" } }]
      }
    }
    billing = {
      apiVersion = "batch/v1", kind = "CronJob", metadata = { name = "billing", namespace = "api" }
      spec = {
        schedule = "*/2 * * * *", concurrencyPolicy = "Forbid", successfulJobsHistoryLimit = 1, failedJobsHistoryLimit = 3
        jobTemplate = { spec = {
          backoffLimit = 1, activeDeadlineSeconds = 110, ttlSecondsAfterFinished = 600
          template = {
            metadata = { labels = { app = "billing" } }
            spec = {
              restartPolicy = "Never", serviceAccountName = "api"
              containers = [{
                name = "billing", image = local.image.api, command = ["python", "-m", "billing"]
                env = concat([
                  { name = "DATABASE_URL", valueFrom = { secretKeyRef = { name = "database", key = "platform_url" } } }, # the charge ledger and the keys' spend (services/api/keys.py)
                  { name = "CATALOG_DIRS", value = "/etc/catalog" },
                  { name = "HUB_REGION", value = local.hub_region },
                  ], local.dedicated ? [
                  { name = "REGION", value = "control" },
                  { name = "REGION_KUBECONFIGS", value = join(",", [for cid, c in local.region_clusters : "${c.region}=/etc/kubeconfigs/${c.region}"]) },
                  { name = "FLEET_MANAGER", value = "true" },
                  ] : [
                  { name = "REGION", value = local.region },
                ])
                volumeMounts = concat([{ name = "kubeconfigs", mountPath = "/etc/kubeconfigs", readOnly = true }, { name = "catalog", mountPath = "/etc/catalog", readOnly = true }], local.trust_mounts)
                resources    = { requests = { cpu = "50m", memory = "128Mi" }, limits = { cpu = "500m", memory = "256Mi" } }
              }]
              volumes = concat([{ name = "kubeconfigs", secret = { secretName = "region-kubeconfigs", optional = true } }, { name = "catalog", configMap = { name = "catalog" } }], local.trust_volumes)
            }
          }
        } }
      }
    }
  } : k => v if local.role.control }
}

resource "kubectl_manifest" "extras1" {
  for_each          = local.extras_wave1
  yaml_body         = yamlencode(each.value)
  server_side_apply = true
  force_conflicts   = true
  wait              = false
  depends_on        = [helm_release.wave1, kubectl_manifest.wave1]
}

resource "kubectl_manifest" "weights_pvc" {
  for_each          = local.weights_pvc
  yaml_body         = yamlencode(each.value)
  server_side_apply = true
  force_conflicts   = true
  wait              = false
  depends_on        = [kubectl_manifest.extras1]
  lifecycle { ignore_changes = [yaml_body] }
}

resource "kubectl_manifest" "control_extras" {
  for_each          = local.extras_control
  yaml_body         = yamlencode(each.value)
  server_side_apply = true
  force_conflicts   = true
  wait              = false
  depends_on        = [helm_release.wave3, kubectl_manifest.wave3]
}
