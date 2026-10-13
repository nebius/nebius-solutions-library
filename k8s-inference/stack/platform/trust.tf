# CA certificates are public configuration. HTTPS clients default to the image's system roots plus the CAs under
# /etc/ssl/msp: the Nebius MSP CA (managed PostgreSQL signs its server certificates with it, fetched from
# docs.nebius.com/postgresql/databases/connect) and the fleet CA (the cloud stage generates it; the GPU regions'
# gateways carry certificates it signed, edge.regional_certificates). Private deployments add a complete bundle
# (`trust_bundle_pem`) that replaces the system roots. Never an insecure TLS switch.
data "http" "msp_ca" {
  url = local.f.msp_ca_url
  lifecycle {
    postcondition {
      condition     = self.status_code == 200 && strcontains(self.response_body, "BEGIN CERTIFICATE")
      error_message = "${local.f.msp_ca_url} did not return a PEM certificate (HTTP ${self.status_code}); the trust bundle is not written from an error page."
    }
  }
}

locals {
  msp_ca_path       = "/etc/ssl/msp/msp-ca.pem"
  trust_bundle_path = "/etc/ssl/msp/bundle.pem" # msp + fleet (+ the private bundle): for clients that take one file
  # `ca.crt`: the same private bundle under the key a Gateway API BackendTLSPolicy reads (charts/platform/templates/edge:
  # a GPU region's gateway validates the fleet API's hostname against it in internal mode)
  trust_data = merge(
    { "msp-ca.pem" = data.http.msp_ca.response_body, "fleet-ca.pem" = local.cloud.fleet_ca.cert },
    local.f.trust_bundle_pem == null ? {} : { "ca.pem" = local.f.trust_bundle_pem, "ca.crt" = local.f.trust_bundle_pem },
  )
}

resource "kubernetes_config_map_v1" "trust" {
  for_each = toset(concat(["api"], local.role.control ? ["litellm"] : []))
  metadata {
    name      = "trust"
    namespace = each.key
  }
  data       = merge(local.trust_data, { "bundle.pem" = join("\n", [for k in sort(keys(local.trust_data)) : local.trust_data[k]]) })
  depends_on = [kubernetes_namespace_v1.ns]
}

# The fleet CA key pair for cert-manager's `fleet-ca` ClusterIssuer (manifests.tf) on the GPU regions. The volume and
# mount shapes of the `trust` ConfigMap are stack/config's (`trust_volumes`, `trust_mounts`): charts/platform mounts them
# into the API, the stage into LiteLLM and the billing CronJob.
resource "kubernetes_secret_v1" "fleet_ca" {
  count = local.fleet_ca_here ? 1 : 0
  metadata {
    name      = "fleet-ca"
    namespace = "cert-manager"
  }
  type       = "kubernetes.io/tls"
  data       = { "tls.crt" = local.cloud.fleet_ca.cert, "tls.key" = local.cloud.fleet_ca.key }
  depends_on = [helm_release.wave0]
}

# When the fleet CA changes (regenerated, or the cloud state rewritten by an older checkout), the
# leaf certificates cert-manager issued from the previous CA stay valid for their holders but no longer match the
# trust bundle: this re-issues them (cert-manager recreates a deleted TLS Secret at once) on every CA change.
resource "terraform_data" "reissue_on_ca_change" {
  count = local.fleet_ca_here ? 1 : 0
  input = {
    ca_sha  = sha256(local.cloud.fleet_ca.cert)
    script  = "${path.module}/../scripts/kube.sh"
    server  = local.cluster.endpoint
    ca      = local.cluster.cluster_ca_certificate
    secrets = "${local.id}-wildcard-tls models-tls"
  }
  triggers_replace = [sha256(local.cloud.fleet_ca.cert)]
  provisioner "local-exec" {
    command     = "${self.input.script} -n envoy-gateway-system delete secret ${self.input.secrets} --ignore-not-found"
    environment = { KUBE_SERVER = self.input.server, KUBE_CA = self.input.ca }
  }
  depends_on = [kubernetes_secret_v1.fleet_ca, kubectl_manifest.wave3]
}
