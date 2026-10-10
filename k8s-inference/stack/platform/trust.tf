# CA certificates are public configuration. HTTPS clients default to the image's system roots plus the CAs under
# /etc/ssl/msp: the Nebius MSP CA (managed PostgreSQL signs its server certificates with it, fetched from
# docs.nebius.com/postgresql/databases/connect) and the fleet CA (the cloud stage generates it; the GPU regions'
# gateways carry certificates it signed, edge.regional_certificates). Private deployments add a complete bundle
# (`trust_bundle_pem`) that replaces the system roots. Never an insecure TLS switch.
data "http" "msp_ca" {
  url = "https://storage.eu-north1.nebius.cloud/msp-certs/ca.pem"
}

locals {
  msp_ca_path       = "/etc/ssl/msp/msp-ca.pem"
  trust_bundle_path = "/etc/ssl/msp/bundle.pem" # msp + fleet (+ the private bundle): for clients that take one file
  trust_data = merge(
    { "msp-ca.pem" = data.http.msp_ca.response_body, "fleet-ca.pem" = local.cloud.fleet_ca.cert },
    local.f.trust_bundle_pem == null ? {} : { "ca.pem" = local.f.trust_bundle_pem },
  )
  trust_volumes = [{ name = "trust", configMap = { name = "trust" } }]
  trust_mounts = concat(
    [{ name = "trust", mountPath = "/etc/ssl/msp", readOnly = true }],
    local.f.trust_bundle_pem == null ? [] : [{ name = "trust", mountPath = "/etc/ssl/certs/ca-certificates.crt", subPath = "ca.pem", readOnly = true }],
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

# The fleet CA key pair for cert-manager's `fleet-ca` ClusterIssuer (manifests.tf) on the GPU regions.
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
