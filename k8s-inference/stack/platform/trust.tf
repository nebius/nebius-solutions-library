# CA certificates are public configuration. HTTPS clients default to the image's system roots; private deployments
# provide a complete trust bundle (`trust_bundle_pem`) for them, never an insecure TLS switch. The managed
# PostgreSQL service signs its server certificates with the Nebius MSP CA (docs.nebius.com/postgresql/databases/connect),
# which is not in any system bundle: the stage fetches it and mounts it as /etc/ssl/msp/ca.pem for libpq, Prisma
# and the database-init Job, so every database connection verifies the certificate and the hostname.
data "http" "msp_ca" {
  url = "https://storage.eu-north1.nebius.cloud/msp-certs/ca.pem"
}

locals {
  msp_ca_path   = "/etc/ssl/msp/ca.pem"
  trust_volumes = [{ name = "trust", configMap = { name = "trust" } }]
  trust_mounts = concat(
    [{ name = "trust", mountPath = local.msp_ca_path, subPath = "msp-ca.pem", readOnly = true }],
    local.f.trust_bundle_pem == null ? [] : [{ name = "trust", mountPath = "/etc/ssl/certs/ca-certificates.crt", subPath = "ca.pem", readOnly = true }],
  )
}

resource "kubernetes_config_map_v1" "trust" {
  for_each = toset(concat(["api"], local.role.control ? ["litellm"] : []))
  metadata {
    name      = "trust"
    namespace = each.key
  }
  data = merge(
    { "msp-ca.pem" = data.http.msp_ca.response_body },
    local.f.trust_bundle_pem == null ? {} : { "ca.pem" = local.f.trust_bundle_pem },
  )
  depends_on = [kubernetes_namespace_v1.ns]
}
