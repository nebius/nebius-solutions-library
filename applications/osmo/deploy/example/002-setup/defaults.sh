# =============================================================================
# Default Configuration for Setup Scripts
# =============================================================================

# Namespaces
export GPU_OPERATOR_NAMESPACE="gpu-operator"
export NETWORK_OPERATOR_NAMESPACE="network-operator"
export KAI_SCHEDULER_NAMESPACE="kai-scheduler"
export MONITORING_NAMESPACE="monitoring"
export OSMO_NAMESPACE="osmo"

# Chart versions (leave empty for latest)
export GPU_OPERATOR_VERSION=""
export NETWORK_OPERATOR_VERSION=""
export KAI_SCHEDULER_VERSION="v0.12.4"  # Check https://github.com/NVIDIA/KAI-Scheduler/releases
export PROMETHEUS_VERSION=""
export GRAFANA_VERSION=""
export LOKI_VERSION=""

# GPU Operator settings
export GPU_DRIVER_ENABLED="false"  # Use Nebius driver-full images
export TOOLKIT_ENABLED="true"
export DEVICE_PLUGIN_ENABLED="true"
export MIG_MANAGER_ENABLED="false"

# Network Operator (only needed for InfiniBand/GPU clusters)
export ENABLE_NETWORK_OPERATOR="false"  # Set to "true" if using InfiniBand

# Observability settings
export PROMETHEUS_RETENTION_DAYS="15"
export LOKI_RETENTION_DAYS="7"
export GRAFANA_ADMIN_PASSWORD=""  # Auto-generated if empty

# NGINX Ingress Controller (deployed by 03-deploy-nginx-ingress.sh)
# Namespace where the NGINX Ingress Controller is deployed.
export INGRESS_NAMESPACE="${INGRESS_NAMESPACE:-ingress-nginx}"
# Hostname for Ingress rules (e.g. osmo.example.com). Leave empty to use the LoadBalancer IP directly.
export OSMO_INGRESS_HOSTNAME="${OSMO_INGRESS_HOSTNAME:-}"
# Override for the service_base_url used by osmo-ctrl. Auto-detected from the ingress LoadBalancer if empty.
export OSMO_INGRESS_BASE_URL="${OSMO_INGRESS_BASE_URL:-}"

# TLS / SSL Configuration
# TLS enabled by default. Requires OSMO_INGRESS_HOSTNAME to be set. Set to false to disable.
export OSMO_TLS_ENABLED="${OSMO_TLS_ENABLED:-true}"
# Name of the Kubernetes TLS secret used by Ingress (both paths produce this secret).
# NOTE: The OSMO Helm chart generates ingress TLS with secretName "osmo-tls".
export OSMO_TLS_SECRET_NAME="${OSMO_TLS_SECRET_NAME:-osmo-tls}"
# Local directory where certbot stores certificate files (Path A only).
export OSMO_TLS_CERT_DIR="${OSMO_TLS_CERT_DIR:-$HOME/.osmo-certs}"
# Email for Let's Encrypt registration (required for 03a and 03c).
export LETSENCRYPT_EMAIL="${LETSENCRYPT_EMAIL:-}"
# cert-manager namespace (Path B / 03c only).
export CERT_MANAGER_NAMESPACE="${CERT_MANAGER_NAMESPACE:-cert-manager}"
# Name of the ClusterIssuer created by 03c (Path B only).
export CLUSTER_ISSUER_NAME="${CLUSTER_ISSUER_NAME:-letsencrypt-prod}"
# TLS mode: "certbot" or "cert-manager". Default cert-manager so 04-enable-tls.sh runs without prompting.
export OSMO_TLS_MODE="${OSMO_TLS_MODE:-cert-manager}"

# Keycloak / Authentication
# Keycloak deployed by default. Requires OSMO_INGRESS_HOSTNAME or KEYCLOAK_HOSTNAME. Set to false to disable.
export DEPLOY_KEYCLOAK="${DEPLOY_KEYCLOAK:-true}"
# Keycloak hostname. Use a hostname that resolves from inside the cluster (so Envoy can fetch JWKS).
# Recommended: nip.io (no /etc/hosts) e.g. auth-osmo.<LB_IP_DASHED>.nip.io (see applications/osmo/iam-register/README.md).
# Or: auth-osmo.local (add to /etc/hosts on your machine only; pods need a resolvable hostname or you get 502).
# Auto-derived from OSMO_INGRESS_HOSTNAME if set and KEYCLOAK_HOSTNAME empty: auth-<OSMO_INGRESS_HOSTNAME>.
export KEYCLOAK_HOSTNAME="${KEYCLOAK_HOSTNAME:-}"
# TLS secret name for the Keycloak ingress (separate from the main osmo-tls).
# Run 03a with OSMO_TLS_SECRET_NAME=osmo-tls-auth for the auth subdomain.
export KEYCLOAK_TLS_SECRET_NAME="${KEYCLOAK_TLS_SECRET_NAME:-osmo-tls-auth}"

# Nebius System SSO (additional IdP when enabled)
# When true, Keycloak registers Nebius SSO as an IdP. The Keycloak login page will show a
# "Nebius SSO" button alongside the local login form (authenticateByDefault=false).
# Local login is NOT removed — a local osmo-admin user is still created by default so that
# backend automation (06-deploy-osmo-backend.sh) can obtain a service token via password grant.
# To suppress the local user, set CREATE_OSMO_TEST_USER=false explicitly.
# Requires: NEBIUS_SSO_ISSUER_URL, NEBIUS_SSO_CLIENT_ID, NEBIUS_SSO_CLIENT_SECRET (or keycloak-nebius-sso-secret in cluster).
export NEBIUS_SSO_ENABLED="${NEBIUS_SSO_ENABLED:-false}"
# OIDC issuer URL of Nebius SSO. Use prod for auth: https://auth.nebius.com (beta: https://auth.beta.nebius.ai).
export NEBIUS_SSO_ISSUER_URL="${NEBIUS_SSO_ISSUER_URL:-}"
export NEBIUS_SSO_CLIENT_ID="${NEBIUS_SSO_CLIENT_ID:-}"
# Client secret. Prefer storing in K8s secret keycloak-nebius-sso-secret (key: client_secret); or set here.
export NEBIUS_SSO_CLIENT_SECRET="${NEBIUS_SSO_CLIENT_SECRET:-}"
# Attribute/claim from IdP used for group/role mapping (e.g. "groups", "member_of"). Optional.
export NEBIUS_SSO_GROUP_ATTRIBUTE="${NEBIUS_SSO_GROUP_ATTRIBUTE:-groups}"

# Paths
export SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# Optional one-file deploy config: copy osmo-deploy.env.example to osmo-deploy.env and set hostnames + Nebius SSO + DB password
if [[ -f "${SCRIPT_DIR}/osmo-deploy.env" ]]; then set -a; source "${SCRIPT_DIR}/osmo-deploy.env"; set +a; fi
export VALUES_DIR="${SCRIPT_DIR}/values"
export LIB_DIR="${SCRIPT_DIR}/lib"

# Create a local test user (osmo-admin). When Nebius SSO is enabled and this
# is unset, default to true so backend deploy (06) can always obtain a service
# token via password grant without manual tweaks. This means local login remains
# available even with SSO enabled — set CREATE_OSMO_TEST_USER=false explicitly
# to suppress it (you must then supply OSMO_KC_ADMIN_USER/OSMO_KC_ADMIN_PASS for 06).
# This logic runs *after* loading osmo-deploy.env so NEBIUS_SSO_ENABLED from
# that file is respected.
export CREATE_OSMO_TEST_USER="${CREATE_OSMO_TEST_USER:-}"
if [[ "${NEBIUS_SSO_ENABLED:-false}" == "true" && -z "${CREATE_OSMO_TEST_USER:-}" ]]; then
  export CREATE_OSMO_TEST_USER="true"
fi

# Auto-detect OSMO_INGRESS_HOSTNAME from LoadBalancer IP when using nip.io (so UI is reachable)
# Run after osmo-deploy.env so we can override a stale or empty hostname.
_osmo_lb_ip=$(kubectl get svc -n "${INGRESS_NAMESPACE:-ingress-nginx}" \
    -l app.kubernetes.io/name=ingress-nginx,app.kubernetes.io/component=controller \
    -o jsonpath='{.items[0].status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
if [[ -n "$_osmo_lb_ip" ]]; then
    _osmo_lb_dashed="${_osmo_lb_ip//./-}"
    if [[ -z "${OSMO_INGRESS_HOSTNAME:-}" ]]; then
        export OSMO_INGRESS_HOSTNAME="osmo.${_osmo_lb_dashed}.nip.io"
    elif [[ "${OSMO_INGRESS_HOSTNAME}" == osmo.*.nip.io ]]; then
        _osmo_current_dashed="${OSMO_INGRESS_HOSTNAME#osmo.}"
        _osmo_current_dashed="${_osmo_current_dashed%%.nip.io}"
        _osmo_current_ip="${_osmo_current_dashed//-/.}"
        if [[ "$_osmo_current_ip" != "$_osmo_lb_ip" ]]; then
            export OSMO_INGRESS_HOSTNAME="osmo.${_osmo_lb_dashed}.nip.io"
            export KEYCLOAK_HOSTNAME="auth-osmo.${_osmo_lb_dashed}.nip.io"
            export OSMO_NIPIO_HOSTNAME_CORRECTED="true"
        fi
    fi
fi
unset _osmo_lb_ip _osmo_lb_dashed _osmo_current_dashed _osmo_current_ip 2>/dev/null || true

# Keycloak hostname: auto-derive from OSMO_INGRESS_HOSTNAME when unset (e.g. osmo.89-169-122-246.nip.io -> auth-osmo.89-169-122-246.nip.io)
if [[ -z "${KEYCLOAK_HOSTNAME:-}" && -n "${OSMO_INGRESS_HOSTNAME:-}" ]]; then export KEYCLOAK_HOSTNAME="auth-${OSMO_INGRESS_HOSTNAME}"; fi
