locals {
  # The saturn-helm-operator-nebius chart nests all operator settings under the
  # "saturn-helm-operator" subchart key. instanceConfig is NOT set here: the chart owns
  # it via regionInstanceConfigs[region], so we just pass the region (required by the
  # chart) and let it pick the matching per-region instance sizes.
  saturn_helm_values = {
    "saturn-helm-operator" = merge(
      # Optional image overrides (dev/iteration; production gets images injected at
      # chart-build time). e.g. {saturnEnterprise = "...:2026.02.01-74"}.
      length(var.image_overrides) > 0 ? { images = var.image_overrides } : {},
      # Only pass domain/baseUrl/sshDomain when saturn_domain is set explicitly;
      # otherwise the chart derives all three from customerName.
      var.saturn_domain != "" ? {
        domain    = var.saturn_domain
        baseUrl   = local.saturn_base_url
        sshDomain = local.saturn_ssh_domain
      } : {},
      {
      # Create dependent namespaces (saturn, ingress, cert-manager, logging, monitoring)
      createNamespaces = true

      # Core Saturn configuration
      clusterName      = var.cluster_name
      cloudProvider    = local.saturn_cloud_provider
      region           = var.region
      availabilityZone = var.region

      # Additional Saturn configuration
      adminEmail         = var.saturn_admin_email
      customerName       = var.saturn_customer_name
      imageBuildNodeRole = local.saturn_image_build_node_role
      bootstrapToken     = var.saturn_bootstrap_token

      # Saturn components configuration
      saturnComponents = {
        # The base saturn-helm-operator chart templates reference jobset, phoebe and
        # spegel, but its default saturnComponents values omit all three — so they
        # nil-deref unless set explicitly. (Chart bug: base chart should default these
        # three / guard the derefs.) jobset+phoebe off; spegel is enabled by the Nebius
        # wrapper but ships no values block, so supply an empty one.
        jobset = {
          enabled = false
        }
        phoebe = {
          enabled = false
        }
        spegel = {
          enabled = true
          values  = {}
        }
        atlas = {
          values = {
            env = merge(
              {
                USE_AWS_SHIM = "false"
              },
              var.enable_filestore ? {
                NETWORK_FILESYSTEM_ENABLED           = "true"
                NETWORK_FILESYSTEM_DEFAULT           = "fss"
                NETWORK_FILESYSTEM_FSS_STORAGE_CLASS = "csi-mounted-fs-path-sc"
              } : {}
            )
          }
        }
        clusterSetup = {
          enabled = true
          values = {
            nvidiaDevicePluginEnabled = false
            gpuNodeLabel              = "nvidia.com/gpu"
            dockerSecrets = {
              enabled          = true
              targetNamespaces = "cert-manager,default,ingress,kube-system,logging,main-namespace,monitoring,saturn"
            }
          }
        }
      }
      }
    )
  }
}

resource "helm_release" "saturn_operator" {
  count = var.manage_helm ? 1 : 0

  name = "saturn-helm-operator"
  # When helm_chart_local_path is set, install the chart from that local directory
  # (run `helm dependency build` on it first); otherwise pull the published chart from
  # the OCI registry at helm_chart_version.
  repository       = var.helm_chart_local_path != "" ? null : "oci://ghcr.io/saturncloud/charts"
  chart            = var.helm_chart_local_path != "" ? var.helm_chart_local_path : "saturn-helm-operator-nebius"
  version          = var.helm_chart_local_path != "" ? null : var.helm_chart_version
  namespace        = "saturn-system"
  create_namespace = true

  values = [yamlencode(local.saturn_helm_values)]

  depends_on = [
    nebius_mk8s_v1_cluster.saturn_cluster
  ]
}

output "saturn_helm_values_yaml" {
  description = "The values.yaml content used for the Saturn helm chart"
  value       = yamlencode(local.saturn_helm_values)
  sensitive   = true
}
