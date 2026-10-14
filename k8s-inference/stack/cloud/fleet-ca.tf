# The fleet CA: signs the certificates of the regional gateways (platform stage, cert-manager ClusterIssuer
# `fleet-ca`), so a region needs no public reachability of its gateway for ACME HTTP-01; the control plane and
# LiteLLM trust it through the platform stage's trust bundle. The control cluster's own public hostnames keep
# ACME. The key lives in this state (the state bucket is private; the database password is there too).
resource "tls_private_key" "fleet_ca" {
  algorithm   = "ECDSA"
  ecdsa_curve = "P256"
}

resource "tls_self_signed_cert" "fleet_ca" {
  private_key_pem       = tls_private_key.fleet_ca.private_key_pem
  is_ca_certificate     = true
  validity_period_hours = 87600 # ten years; cert-manager renews the leaf certificates
  early_renewal_hours   = 8760
  allowed_uses          = ["cert_signing", "crl_signing", "digital_signature"]
  subject {
    common_name  = "${local.f.name} fleet CA"
    organization = "serverless2"
  }
}

output "fleet_ca" {
  value     = { cert = tls_self_signed_cert.fleet_ca.cert_pem, key = tls_private_key.fleet_ca.private_key_pem }
  sensitive = true
}
