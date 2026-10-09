#!/bin/bash
#
# Create self-signed TLS secrets for osmo.local and auth-osmo.local.
# Use this when testing Nebius SSO with .local hostnames (Let's Encrypt
# cannot issue for .local). After running this, re-run 04-deploy-osmo-control-plane.sh
# so Keycloak gets an external ingress and Envoy auth is enabled.
#
# Prerequisites: kubectl, openssl. Run from 002-setup.
#
# Usage: ./create-local-tls-secrets.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
source "${SCRIPT_DIR}/defaults.sh" 2>/dev/null || true

OSMO_NS="${OSMO_NAMESPACE:-osmo}"
INGRESS_NS="${INGRESS_NAMESPACE:-ingress-nginx}"
MAIN_HOST="${OSMO_INGRESS_HOSTNAME:-osmo.local}"
AUTH_HOST="${KEYCLOAK_HOSTNAME:-auth-osmo.local}"
MAIN_SECRET="${OSMO_TLS_SECRET_NAME:-osmo-tls}"
AUTH_SECRET="${KEYCLOAK_TLS_SECRET_NAME:-osmo-tls-auth}"
CERT_DIR="${SCRIPT_DIR}/.local-tls-certs"

echo ""
echo "========================================"
echo "  Local TLS secrets (.local)"
echo "========================================"
echo ""
echo "Main hostname:  ${MAIN_HOST}  (secret: ${MAIN_SECRET})"
echo "Auth hostname:  ${AUTH_HOST}  (secret: ${AUTH_SECRET})"
echo ""

if ! command -v openssl &>/dev/null; then
    echo "Error: openssl is required."
    exit 1
fi
if ! command -v kubectl &>/dev/null; then
    echo "Error: kubectl is required."
    exit 1
fi

mkdir -p "$CERT_DIR"
cd "$CERT_DIR"

gen_cert() {
    local name="$1"
    local cn="$2"
    local san="$3"
    if [[ -f "${name}.crt" && -f "${name}.key" ]]; then
        echo "  ${name}.crt/key already exist; skipping"
        return 0
    fi
    echo "  Generating self-signed cert for ${cn}..."
    local conf="${name}.cnf"
    cat > "$conf" <<EOF
[req]
distinguished_name = dn
req_extensions = ext
[dn]
CN = ${cn}
[ext]
subjectAltName = DNS:${san}
EOF
    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
        -keyout "${name}.key" -out "${name}.crt" \
        -subj "/CN=${cn}" -extensions ext -config "$conf"
}

create_tls_secret() {
    local secret_name="$1"
    local cert_file="$2"
    local key_file="$3"
    local ns="$4"
    if [[ ! -f "$cert_file" || ! -f "$key_file" ]]; then
        echo "  Error: missing $cert_file or $key_file"
        return 1
    fi
    kubectl create secret tls "$secret_name" \
        --cert="$cert_file" --key="$key_file" \
        -n "$ns" --dry-run=client -o yaml | kubectl apply -f -
    echo "  Created/updated secret $secret_name in $ns"
}

echo "Generating certificates..."
gen_cert "osmo" "$MAIN_HOST" "$MAIN_HOST"
gen_cert "auth" "$AUTH_HOST" "$AUTH_HOST"

echo ""
echo "Creating TLS secrets (${OSMO_NS} and ${INGRESS_NS})..."
create_tls_secret "$MAIN_SECRET" "osmo.crt" "osmo.key" "$OSMO_NS"
create_tls_secret "$MAIN_SECRET" "osmo.crt" "osmo.key" "$INGRESS_NS"
create_tls_secret "$AUTH_SECRET" "auth.crt" "auth.key" "$OSMO_NS"
create_tls_secret "$AUTH_SECRET" "auth.crt" "auth.key" "$INGRESS_NS"

echo ""
echo "Done. Next steps:"
echo "  1. Add to /etc/hosts:  <LOADBALANCER_IP>  ${MAIN_HOST} ${AUTH_HOST}"
echo "  2. Export Nebius SSO env vars (NEBIUS_SSO_ENABLED, CLIENT_ID, CLIENT_SECRET, etc.)"
echo "  3. Re-run: ./04-deploy-osmo-control-plane.sh"
echo "  4. Open https://${MAIN_HOST} and accept the self-signed cert; you should be redirected to Nebius SSO."
echo ""
