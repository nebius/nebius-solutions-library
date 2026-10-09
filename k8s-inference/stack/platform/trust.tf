# CA certificates are public configuration. Default to image system roots; private deployments provide
# a complete trust bundle for both HTTPS clients and PostgreSQL, never an insecure TLS switch.
locals {
  trust_volumes = local.f.trust_bundle_pem == null ? [] : [{ name = "trust", configMap = { name = "trust" } }]
  trust_mounts  = local.f.trust_bundle_pem == null ? [] : [{ name = "trust", mountPath = "/etc/ssl/certs/ca-certificates.crt", subPath = "ca.pem", readOnly = true }]
}

resource "kubernetes_config_map_v1" "trust" {
  for_each = local.f.trust_bundle_pem == null ? toset([]) : toset(concat(["api"], local.role.control ? ["litellm"] : []))
  metadata {
    name      = "trust"
    namespace = each.key
  }
  data       = { "ca.pem" = local.f.trust_bundle_pem }
  depends_on = [kubernetes_namespace_v1.ns]
}
