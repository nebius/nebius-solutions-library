# Shared manifests (clusters/common/manifests/<component>) rendered through kustomize with this cluster's
# patches (stage.tf `patches`), applied server-side in the same waves as the charts.
data "kustomization_overlay" "component" {
  for_each  = local.manifest_apps
  resources = [local.manifests_base[each.key]]
  kustomize_options {
    load_restrictor = "none"
  }
  dynamic "patches" {
    for_each = try(local.patches[each.key], [])
    content {
      patch = patches.value.patch
      target {
        kind      = try(patches.value.target.kind, null)
        name      = try(patches.value.target.name, null)
        namespace = try(patches.value.target.namespace, null)
      }
    }
  }
}

locals {
  # The `models` certificate (clusters/common/manifests/gateway/models-tls.yaml): its hostnames and issuer
  # follow the models defined through the API, which rewrites them; Terraform only creates the placeholder.
  models_certificate        = "Certificate[/|]envoy-gateway-system[/|]models$"
  models_certificate_fields = ["spec.dnsNames", "spec.issuerRef", "spec.commonName"]
  manifests_wave = { for w in ["0", "1", "2", "3"] : w => merge([
    for n, a in local.manifest_apps : data.kustomization_overlay.component[n].manifests if tostring(a.wave) == w
  ]...) }
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
          issuerRef  = { name = local.f.edge.acme.staging ? "letsencrypt-staging" : "letsencrypt", kind = "ClusterIssuer" }
          commonName = local.hostnames.api
          dnsNames   = distinct(values(local.hostnames))
          privateKey = { rotationPolicy = "Always" }
        }
      }
    },
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
      weights_pvc = {
        apiVersion = "v1", kind = "PersistentVolumeClaim"
        metadata   = { name = "weights-shared", namespace = "models" }
        spec = {
          accessModes = ["ReadWriteMany"], storageClassName = "weights-shared", volumeName = "weights-shared"
          resources   = { requests = { storage = "${local.cluster.weights_filesystem.size_gib}Gi" } }
        }
      }
    } : k => v if local.cluster.weights_filesystem.enabled },
  )

  # Control plane only: the console, the billing CronJob, LiteLLM UI route.
  extras_control = { for k, v in {
    ui_app = {
      apiVersion = "apps/v1", kind = "Deployment"
      metadata   = { name = "ui-app", namespace = "ui-app", labels = { app = "ui-app" } }
      spec = {
        replicas = 2, selector = { matchLabels = { app = "ui-app" } }
        template = {
          metadata = { labels = { app = "ui-app" } }
          spec = {
            securityContext = { runAsNonRoot = true, runAsUser = 101, seccompProfile = { type = "RuntimeDefault" } }
            containers = [{
              name = "nginx", image = local.image.ui, ports = [{ name = "http", containerPort = 8080 }]
              env = [
                { name = "API_UPSTREAM", value = "http://api.api.svc.cluster.local:80" },
                { name = "DNS_RESOLVER", value = data.kubernetes_service_v1.coredns.spec[0].cluster_ip },
                # served to the browser at /config.json (ui/src/config.ts): the public API and the Grafana of every cluster
                { name = "PUBLIC_API_URL", value = "https://${local.hostnames.api}" },
                { name = "GRAFANA_URLS", value = local.grafana_urls },
              ]
              resources       = { requests = { cpu = "20m", memory = "32Mi" }, limits = { cpu = "200m", memory = "128Mi" } }
              readinessProbe  = { httpGet = { path = "/healthz", port = "http" }, periodSeconds = 5 }
              livenessProbe   = { httpGet = { path = "/healthz", port = "http" }, periodSeconds = 20 }
              securityContext = { allowPrivilegeEscalation = false, capabilities = { drop = ["ALL"] } }
            }]
          }
        }
      }
    }
    ui_app_svc = {
      apiVersion = "v1", kind = "Service", metadata = { name = "ui-app", namespace = "ui-app" }
      spec       = { selector = { app = "ui-app" }, ports = [{ name = "http", port = 80, targetPort = "http" }] }
    }
    ui_app_route = {
      apiVersion = "gateway.networking.k8s.io/v1", kind = "HTTPRoute", metadata = { name = "ui-app", namespace = "ui-app" }
      spec = {
        parentRefs = [{ name = "serverless2-external", namespace = "envoy-gateway-system", sectionName = "https" }]
        hostnames  = [try(local.hostnames.app, "")], rules = [{ backendRefs = [{ name = "ui-app", port = 80 }] }]
      }
    }
    litellm_route = {
      apiVersion = "gateway.networking.k8s.io/v1", kind = "HTTPRoute", metadata = { name = "ui-litellm", namespace = "litellm" }
      spec = {
        parentRefs = [{ name = "serverless2-external", namespace = "envoy-gateway-system", sectionName = "https" }]
        hostnames  = [try(local.hostnames.litellm, "")], rules = [{ backendRefs = [{ name = "litellm", port = 4000 }] }]
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
                  { name = "LITELLM_URL", value = local.litellm_url_in_cluster },
                  { name = "LITELLM_MASTER_KEY", valueFrom = { secretKeyRef = { name = "litellm-master", key = "masterkey" } } },
                  { name = "CATALOG_DIRS", value = "/etc/catalog" },
                  { name = "HUB_REGION", value = local.hub_region },
                  ], local.dedicated ? [
                  { name = "REGION", value = "control" },
                  { name = "REGION_KUBECONFIGS", value = join(",", [for cid, c in local.region_clusters : "${c.region}=/etc/kubeconfigs/${c.region}"]) },
                  { name = "FLEET_MANAGER", value = "true" },
                  ] : [
                  { name = "REGION", value = local.region },
                ])
                volumeMounts = [{ name = "kubeconfigs", mountPath = "/etc/kubeconfigs", readOnly = true }, { name = "catalog", mountPath = "/etc/catalog", readOnly = true }]
                resources    = { requests = { cpu = "50m", memory = "128Mi" }, limits = { cpu = "500m", memory = "256Mi" } }
              }]
              volumes = [{ name = "kubeconfigs", secret = { secretName = "region-kubeconfigs", optional = true } }, { name = "catalog", configMap = { name = "catalog" } }]
            }
          }
        } }
      }
    }
  } : k => v if local.role.control }
}

data "kubernetes_service_v1" "coredns" {
  metadata {
    name      = "coredns"
    namespace = "kube-system"
  }
}

resource "kubectl_manifest" "extras1" {
  for_each          = local.extras_wave1
  yaml_body         = yamlencode(each.value)
  server_side_apply = true
  force_conflicts   = true
  wait              = false
  depends_on        = [helm_release.wave1, kubectl_manifest.wave1]
}

resource "kubectl_manifest" "control_extras" {
  for_each          = local.extras_control
  yaml_body         = yamlencode(each.value)
  server_side_apply = true
  force_conflicts   = true
  wait              = false
  depends_on        = [helm_release.wave3, kubectl_manifest.wave3]
}
