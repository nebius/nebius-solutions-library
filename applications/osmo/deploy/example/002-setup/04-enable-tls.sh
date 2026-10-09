#!/bin/bash
#
# Enable TLS/HTTPS for OSMO using Let's Encrypt
#
# Supports two certificate methods:
#   1) cert-manager (default) — automated HTTP-01 challenges via in-cluster cert-manager
#   2) certbot — interactive manual DNS-01 challenges via local certbot binary
#
# Default: OSMO_TLS_MODE=cert-manager (no prompt). Set to certbot to use certbot instead.
#
# Can be run at two points in the deployment flow:
#
#   A) Right after 03-deploy-nginx-ingress.sh (RECOMMENDED):
#      Issues the TLS certificate early. When 04-deploy-osmo-control-plane.sh
#      runs later, it auto-detects the certificate and creates TLS-enabled Ingress.
#
#   B) After 04-deploy-osmo-control-plane.sh (retrofit existing deployment):
#      Does everything in (A) plus patches existing OSMO Ingress resources
#      and updates service_base_url to HTTPS.
#
# Prerequisites:
#   1. NGINX Ingress Controller deployed (03-deploy-nginx-ingress.sh)
#   2. A DNS record pointing your domain to the LoadBalancer IP
#      (A record for cert-manager/HTTP-01; TXT record for certbot/DNS-01)
#
# Usage:
#   ./04-enable-tls.sh [hostname]
#     - If [hostname] is omitted, the script uses OSMO_INGRESS_HOSTNAME.
#
# Optional environment variables:
#   OSMO_TLS_MODE         - "cert-manager" (default) or "certbot"
#   OSMO_TLS_SKIP_DNS_CONFIRM - set to "true" to skip DNS confirmation prompt (auto-set for nip.io hostnames)
#   OSMO_TLS_EMAIL        - Email for Let's Encrypt (default: noreply@<domain>)
#   OSMO_TLS_SECRET_NAME  - K8s Secret name for certificate (default: osmo-tls)
#   LETSENCRYPT_EMAIL     - Alias for OSMO_TLS_EMAIL (certbot path)
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
source "${SCRIPT_DIR}/lib/common.sh"
source "${SCRIPT_DIR}/defaults.sh"

MAIN_HOSTNAME="${1:-${OSMO_INGRESS_HOSTNAME:-}}"
MAIN_HOSTNAME="${MAIN_HOSTNAME%.}"  # Strip trailing dot (FQDN notation)
TLS_SECRET="${OSMO_TLS_SECRET_NAME:-osmo-tls}"
OSMO_NS="${OSMO_NAMESPACE:-osmo}"
INGRESS_NS="${INGRESS_NAMESPACE:-ingress-nginx}"
CERT_DIR="${OSMO_TLS_CERT_DIR:-$HOME/.osmo-certs}"

echo ""
echo "========================================"
echo "  Enable TLS/HTTPS"
echo "========================================"
echo ""

# -----------------------------------------------------------------------------
# Validate inputs
# -----------------------------------------------------------------------------
if [[ -z "$MAIN_HOSTNAME" ]]; then
    log_error "Hostname is required."
    echo ""
    echo "Usage: $0 <hostname>"
    echo "   or: export OSMO_INGRESS_HOSTNAME=osmo.example.com"
    echo ""
    LB_IP=$(kubectl get svc -n "${INGRESS_NS}" ingress-nginx-controller \
        -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
    if [[ -n "$LB_IP" ]]; then
        echo "Your LoadBalancer IP is: ${LB_IP}"
        echo "Create a DNS A record pointing your domain to this IP, then re-run this script."
    fi
    exit 1
fi

check_kubectl || exit 1

log_info "Hostname: ${MAIN_HOSTNAME}"
log_info "TLS secret: ${TLS_SECRET}"

# Keycloak auth subdomain support (auth-<main> e.g. auth-osmo.89-169-122-246.nip.io for nip.io)
KC_TLS_SECRET="${KEYCLOAK_TLS_SECRET_NAME:-osmo-tls-auth}"
AUTH_HOSTNAME=""
if [[ "${DEPLOY_KEYCLOAK:-false}" == "true" ]]; then
    if [[ -n "${KEYCLOAK_HOSTNAME:-}" ]]; then
        AUTH_HOSTNAME="${KEYCLOAK_HOSTNAME}"
    else
        AUTH_HOSTNAME="auth-${MAIN_HOSTNAME}"
    fi
    log_info "Keycloak auth hostname: ${AUTH_HOSTNAME}"
    log_info "Keycloak TLS secret: ${KC_TLS_SECRET}"
fi

# Get LoadBalancer IP
LB_IP=$(kubectl get svc -n "${INGRESS_NS}" ingress-nginx-controller \
    -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)

# -----------------------------------------------------------------------------
# Select TLS method
# -----------------------------------------------------------------------------
TLS_MODE="${OSMO_TLS_MODE:-}"
if [[ -z "$TLS_MODE" ]]; then
    echo ""
    echo "Select TLS certificate method:"
    echo ""
    echo "  1) cert-manager  — automated HTTP-01 challenges (requires DNS A record)"
    echo "  2) certbot       — interactive DNS-01 challenges (requires DNS TXT record)"
    echo ""
    while true; do
        printf "Enter choice [1-2] (default: 1): "
        read -r _tls_choice
        case "${_tls_choice:-1}" in
            1) TLS_MODE="cert-manager"; break ;;
            2) TLS_MODE="certbot"; break ;;
            *) echo "Invalid selection." ;;
        esac
    done
fi

log_info "TLS method: ${TLS_MODE}"

# -----------------------------------------------------------------------------
# DNS info
# -----------------------------------------------------------------------------
echo ""
echo "========================================"
echo "  DNS Record Setup Required"
echo "========================================"
echo ""
if [[ -n "$LB_IP" ]]; then
    echo "Create the following DNS A record(s) pointing to your LoadBalancer IP:"
    echo ""
    echo "  ${MAIN_HOSTNAME}  ->  ${LB_IP}"
    if [[ -n "$AUTH_HOSTNAME" ]]; then
        echo "  ${AUTH_HOSTNAME}  ->  ${LB_IP}"
    fi
else
    echo "LoadBalancer IP not yet assigned. Check with:"
    echo "  kubectl get svc -n ${INGRESS_NS} ingress-nginx-controller"
    echo ""
    echo "Once the IP is available, create DNS A record(s) for:"
    echo "  ${MAIN_HOSTNAME}"
    if [[ -n "$AUTH_HOSTNAME" ]]; then
        echo "  ${AUTH_HOSTNAME}"
    fi
fi
echo ""
if [[ "$TLS_MODE" == "certbot" ]]; then
    echo "Certbot DNS-01 challenges require you to create TXT records when prompted."
else
    echo "Let's Encrypt HTTP-01 challenges require DNS to resolve to the LoadBalancer."
fi
echo ""
# Skip DNS confirmation when using nip.io (resolves automatically) or when OSMO_TLS_SKIP_DNS_CONFIRM is set
if [[ "${MAIN_HOSTNAME}" == *"nip.io"* || "${OSMO_TLS_SKIP_DNS_CONFIRM:-false}" == "true" ]]; then
    DNS_CONFIRM="skip"
    log_info "Skipping DNS confirmation (nip.io or OSMO_TLS_SKIP_DNS_CONFIRM=true)"
else
    read_prompt_var "Press Enter once DNS records are configured (or type 'skip' to skip DNS check)" DNS_CONFIRM ""
fi

# Verify DNS resolves to the LoadBalancer IP
if [[ "$DNS_CONFIRM" != "skip" ]]; then
    DNS_IP=$(dig +short "$MAIN_HOSTNAME" 2>/dev/null | tail -1 || true)

    if [[ -n "$LB_IP" && -n "$DNS_IP" ]]; then
        if [[ "$DNS_IP" == "$LB_IP" ]]; then
            log_success "DNS check: ${MAIN_HOSTNAME} -> ${DNS_IP} (matches LoadBalancer)"
        else
            log_warning "DNS mismatch: ${MAIN_HOSTNAME} -> ${DNS_IP}, but LoadBalancer IP is ${LB_IP}"
            log_warning "Let's Encrypt challenge may fail if DNS doesn't point to the LoadBalancer."
        fi
    elif [[ -z "$DNS_IP" ]]; then
        log_warning "Could not resolve ${MAIN_HOSTNAME}. Make sure the DNS record exists."
    fi

    if [[ -n "$AUTH_HOSTNAME" ]]; then
        AUTH_DNS_IP=$(dig +short "$AUTH_HOSTNAME" 2>/dev/null | tail -1 || true)
        if [[ -n "$LB_IP" && -n "$AUTH_DNS_IP" ]]; then
            if [[ "$AUTH_DNS_IP" == "$LB_IP" ]]; then
                log_success "DNS check: ${AUTH_HOSTNAME} -> ${AUTH_DNS_IP} (matches LoadBalancer)"
            else
                log_warning "DNS mismatch: ${AUTH_HOSTNAME} -> ${AUTH_DNS_IP}, but LoadBalancer IP is ${LB_IP}"
            fi
        elif [[ -z "$AUTH_DNS_IP" ]]; then
            log_warning "Could not resolve ${AUTH_HOSTNAME}. Keycloak TLS cert may fail."
        fi
    fi
fi

# Check if OSMO is already deployed (determines whether to patch Ingress / update config)
INGRESS_COUNT=$(kubectl get ingress -n "${OSMO_NS}" --no-headers 2>/dev/null | wc -l | tr -d ' ')
if [[ "$INGRESS_COUNT" -gt 0 ]]; then
    log_info "Found ${INGRESS_COUNT} Ingress resource(s) in ${OSMO_NS} (will patch with TLS)"
    OSMO_DEPLOYED="true"
else
    log_info "No OSMO Ingress resources yet — preparing certificate"
    log_info "04-deploy-osmo-control-plane.sh will auto-detect the TLS cert"
    OSMO_DEPLOYED="false"
fi

# Ensure the OSMO namespace exists
kubectl create namespace "${OSMO_NS}" --dry-run=client -o yaml | kubectl apply -f -

# =============================================================================
# Helper: create K8s TLS secret in both namespaces from cert/key files
# =============================================================================
create_tls_secret_from_files() {
    local secret_name="$1"
    local cert_path="$2"
    local key_path="$3"

    log_info "Creating TLS secret '${secret_name}' in namespace '${INGRESS_NS}'..."
    kubectl create secret tls "${secret_name}" \
        --cert="${cert_path}" \
        --key="${key_path}" \
        --namespace "${INGRESS_NS}" \
        --dry-run=client -o yaml | kubectl apply -f -

    if [[ "$OSMO_NS" != "$INGRESS_NS" ]]; then
        log_info "Creating TLS secret '${secret_name}' in namespace '${OSMO_NS}'..."
        kubectl create secret tls "${secret_name}" \
            --cert="${cert_path}" \
            --key="${key_path}" \
            --namespace "${OSMO_NS}" \
            --dry-run=client -o yaml | kubectl apply -f -
    fi
    log_success "TLS secret '${secret_name}' created"
}

# =============================================================================
# Helper: copy cert-manager secret to the other namespace if needed
# =============================================================================
copy_secret_across_namespaces() {
    local secret_name="$1"
    if [[ "$OSMO_NS" != "$INGRESS_NS" ]]; then
        # cert-manager creates the secret in the Certificate's namespace (OSMO_NS).
        # Copy it to the ingress namespace so both can reference it.
        if kubectl get secret "${secret_name}" -n "${OSMO_NS}" &>/dev/null; then
            if ! kubectl get secret "${secret_name}" -n "${INGRESS_NS}" &>/dev/null; then
                log_info "Copying secret '${secret_name}' to namespace '${INGRESS_NS}'..."
                kubectl get secret "${secret_name}" -n "${OSMO_NS}" -o json \
                    | jq 'del(.metadata.namespace,.metadata.resourceVersion,.metadata.uid,.metadata.creationTimestamp)' \
                    | kubectl apply -n "${INGRESS_NS}" -f -
            fi
        fi
    fi
}

# #############################################################################
#  CERTBOT PATH
# #############################################################################
if [[ "$TLS_MODE" == "certbot" ]]; then

    # Check certbot
    if ! command -v certbot &>/dev/null; then
        log_error "certbot is not installed."
        echo ""
        echo "Install certbot using one of these methods:"
        echo "  Ubuntu/Debian: sudo apt install certbot"
        echo "  macOS:         brew install certbot"
        echo "  pip:           pip install certbot"
        echo "  snap:          sudo snap install certbot --classic"
        echo ""
        exit 1
    fi
    log_success "certbot found: $(certbot --version 2>&1 | head -1)"

    TLS_EMAIL="${LETSENCRYPT_EMAIL:-${OSMO_TLS_EMAIL:-}}"
    if [[ -z "$TLS_EMAIL" ]]; then
        echo "Enter your email for Let's Encrypt registration:"
        printf "  Email: "
        read -r TLS_EMAIL
        echo ""
        if [[ -z "$TLS_EMAIL" ]]; then
            log_error "Email is required for certbot."
            exit 1
        fi
    fi

    # Build list of domains to process: "domain:secret_name"
    DOMAINS_TO_PROCESS=("${MAIN_HOSTNAME}:${TLS_SECRET}")
    if [[ -n "$AUTH_HOSTNAME" ]]; then
        DOMAINS_TO_PROCESS+=("${AUTH_HOSTNAME}:${KC_TLS_SECRET}")
    fi

    # Show plan
    echo ""
    echo "========================================"
    echo "  Certificate Plan (certbot DNS-01)"
    echo "========================================"
    echo ""
    echo "  Email:          ${TLS_EMAIL}"
    echo "  Cert directory: ${CERT_DIR}"
    echo ""
    echo "  Certificates to obtain:"
    for entry in "${DOMAINS_TO_PROCESS[@]}"; do
        d="${entry%%:*}"
        s="${entry##*:}"
        echo "    ${d}  ->  secret '${s}'"
    done
    echo ""
    if [[ ${#DOMAINS_TO_PROCESS[@]} -gt 1 ]]; then
        echo "  Certbot will run once per domain. Each requires a separate DNS TXT record."
        echo ""
    fi
    printf "  Press Enter to continue (or Ctrl-C to abort)..."
    read -r
    echo ""

    # Process each domain
    FAILED=()
    for entry in "${DOMAINS_TO_PROCESS[@]}"; do
        domain="${entry%%:*}"
        secret_name="${entry##*:}"

        echo ""
        echo "========================================"
        echo "  Certificate: ${domain}"
        echo "  Secret:      ${secret_name}"
        echo "========================================"
        echo ""

        mkdir -p "${CERT_DIR}/work" "${CERT_DIR}/logs"

        echo "Certbot will ask you to create a DNS TXT record."
        echo "When prompted:"
        echo "  1. Log in to your DNS provider"
        echo "  2. Create a TXT record for _acme-challenge.${domain}"
        echo "  3. Wait for DNS propagation (1-5 minutes)"
        echo "  4. Press Enter in this terminal to continue"
        echo ""
        log_info "Starting certbot for ${domain}..."

        if ! certbot certonly \
            --manual \
            --preferred-challenges dns \
            -d "${domain}" \
            --email "${TLS_EMAIL}" \
            --agree-tos \
            --no-eff-email \
            --config-dir "${CERT_DIR}" \
            --work-dir "${CERT_DIR}/work" \
            --logs-dir "${CERT_DIR}/logs"; then
            log_error "certbot failed for ${domain}. Check the output above."
            FAILED+=("$domain")
            continue
        fi

        cert_path="${CERT_DIR}/live/${domain}/fullchain.pem"
        key_path="${CERT_DIR}/live/${domain}/privkey.pem"

        if [[ ! -f "$cert_path" || ! -f "$key_path" ]]; then
            log_error "Certificate files not found for ${domain}."
            echo "  Expected cert: ${cert_path}"
            echo "  Expected key:  ${key_path}"
            FAILED+=("$domain")
            continue
        fi

        log_success "Certificate obtained for ${domain}"
        echo "  Full chain:  ${cert_path}"
        echo "  Private key: ${key_path}"
        echo ""
        log_info "Certificate details:"
        openssl x509 -in "${cert_path}" -noout -subject -issuer -dates 2>/dev/null || true

        # Create K8s TLS secrets in both namespaces
        create_tls_secret_from_files "$secret_name" "$cert_path" "$key_path"
    done

    if [[ ${#FAILED[@]} -gt 0 ]]; then
        log_warning "Some certificates failed:"
        for d in "${FAILED[@]}"; do
            echo "    - ${d}"
        done
        echo "  Fix the issues above and re-run this script."
    fi

# #############################################################################
#  CERT-MANAGER PATH
# #############################################################################
else

    check_helm || exit 1

    # -------------------------------------------------------------------------
    # Install cert-manager
    # -------------------------------------------------------------------------
    log_info "Installing cert-manager..."
    helm repo add jetstack https://charts.jetstack.io --force-update
    helm repo update jetstack

    if helm status cert-manager -n cert-manager &>/dev/null; then
        log_info "cert-manager already installed"
    else
        helm install cert-manager jetstack/cert-manager \
            --namespace cert-manager --create-namespace \
            --set crds.enabled=true \
            --wait --timeout 5m
    fi
    log_success "cert-manager ready"

    # -------------------------------------------------------------------------
    # Create Let's Encrypt ClusterIssuer
    # -------------------------------------------------------------------------
    TLS_EMAIL="${OSMO_TLS_EMAIL:-${LETSENCRYPT_EMAIL:-noreply@${MAIN_HOSTNAME#*.}}}"
    log_info "Creating Let's Encrypt ClusterIssuer (email: ${TLS_EMAIL})..."

    kubectl apply -f - <<EOF
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: ${TLS_EMAIL}
    privateKeySecretRef:
      name: letsencrypt-account-key
    solvers:
      - http01:
          ingress:
            class: nginx
EOF
    log_success "ClusterIssuer created"

    # -------------------------------------------------------------------------
    # Clean up any previous failed certificate attempts
    # -------------------------------------------------------------------------
    log_info "Cleaning up previous certificate attempts (if any)..."
    kubectl delete challenge --all -n "${OSMO_NS}" --ignore-not-found 2>/dev/null || true
    kubectl delete order --all -n "${OSMO_NS}" --ignore-not-found 2>/dev/null || true
    kubectl delete certificaterequest --all -n "${OSMO_NS}" --ignore-not-found 2>/dev/null || true
    kubectl delete certificate "${TLS_SECRET}" -n "${OSMO_NS}" --ignore-not-found 2>/dev/null || true
    kubectl delete secret "${TLS_SECRET}" -n "${OSMO_NS}" --ignore-not-found 2>/dev/null || true
    kubectl delete ingress osmo-tls-bootstrap -n "${OSMO_NS}" --ignore-not-found 2>/dev/null || true
    kubectl delete ingress osmo-tls-auth-bootstrap -n "${OSMO_NS}" --ignore-not-found 2>/dev/null || true
    # Clean up any lingering solver pods from previous attempts
    kubectl delete pods -n "${OSMO_NS}" -l acme.cert-manager.io/http01-solver=true --ignore-not-found 2>/dev/null || true

    # -------------------------------------------------------------------------
    # Helper: wait for a certificate to become ready
    # -------------------------------------------------------------------------
    wait_for_certificate() {
        local cert_name="$1"
        local max_wait="${2:-300}"
        local interval=5
        local elapsed=0

        log_info "Waiting for certificate '${cert_name}' (up to ${max_wait}s)..."
        while [[ $elapsed -lt $max_wait ]]; do
            local cert_status
            cert_status=$(kubectl get certificate "${cert_name}" -n "${OSMO_NS}" \
                -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "")
            if [[ "$cert_status" == "True" ]]; then
                log_success "Certificate '${cert_name}' issued and ready"
                return 0
            fi
            if (( elapsed > 0 && elapsed % 30 == 0 )); then
                local challenge_state
                challenge_state=$(kubectl get challenge -n "${OSMO_NS}" \
                    -o jsonpath='{.items[0].status.state}' 2>/dev/null || echo "unknown")
                log_info "  Still waiting... (elapsed: ${elapsed}s, challenge: ${challenge_state})"
            fi
            sleep $interval
            elapsed=$((elapsed + interval))
        done

        log_warning "Certificate '${cert_name}' not ready after ${max_wait}s"
        kubectl describe certificate "${cert_name}" -n "${OSMO_NS}" 2>/dev/null | tail -15
        echo ""
        echo "Debugging commands:"
        echo "  kubectl get certificate,certificaterequest,order,challenge -n ${OSMO_NS}"
        echo "  kubectl describe challenge -n ${OSMO_NS}"
        return 1
    }

    # -------------------------------------------------------------------------
    # Issue TLS certificate for main domain
    #
    # When OSMO is already deployed (Mode B), the Envoy sidecar on OSMO
    # services intercepts HTTP requests (including the ACME challenge path)
    # and redirects them to Keycloak OAuth, which breaks Let's Encrypt.
    #
    # To work around this, we temporarily remove OSMO Ingress resources
    # that have catch-all paths, create a clean bootstrap Ingress for the
    # challenge, and restore everything with TLS once the cert is ready.
    # -------------------------------------------------------------------------
    REMOVED_INGRESSES=()

    if [[ "$OSMO_DEPLOYED" == "true" ]]; then
        log_info "Temporarily removing OSMO Ingress resources for certificate issuance..."
        log_info "(Envoy sidecars intercept ACME challenges; we need a clean path)"

        # Save and remove all OSMO ingresses to prevent Envoy from intercepting
        mkdir -p /tmp/osmo-tls-backup
        for ing in $(kubectl get ingress -n "${OSMO_NS}" -o name 2>/dev/null); do
            ing_name="${ing#*/}"
            kubectl get "$ing" -n "${OSMO_NS}" -o yaml > "/tmp/osmo-tls-backup/${ing_name}.yaml"
            kubectl delete "$ing" -n "${OSMO_NS}" 2>/dev/null || true
            REMOVED_INGRESSES+=("$ing_name")
            log_info "  Removed ingress/${ing_name} (backed up)"
        done
    fi

    # Create bootstrap Ingress — no Envoy, no auth, just cert-manager
    log_info "Creating bootstrap Ingress for certificate issuance..."
    kubectl apply -f - <<EOF
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: osmo-tls-bootstrap
  namespace: ${OSMO_NS}
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt
    acme.cert-manager.io/http01-edit-in-place: "true"
    nginx.ingress.kubernetes.io/ssl-redirect: "false"
spec:
  ingressClassName: nginx
  tls:
    - hosts:
        - ${MAIN_HOSTNAME}
      secretName: ${TLS_SECRET}
  rules:
    - host: ${MAIN_HOSTNAME}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: osmo-tls-placeholder
                port:
                  number: 80
EOF
    log_success "Bootstrap Ingress created"

    # Wait for main certificate
    CERT_READY="False"
    if wait_for_certificate "${TLS_SECRET}" 300; then
        CERT_READY="True"
    fi

    # Copy main cert secret to ingress namespace if needed
    copy_secret_across_namespaces "${TLS_SECRET}"

    # Always clean up the bootstrap Ingress
    log_info "Removing bootstrap ingress..."
    kubectl delete ingress osmo-tls-bootstrap -n "${OSMO_NS}" --ignore-not-found 2>/dev/null || true

    # -------------------------------------------------------------------------
    # Issue TLS certificate for Keycloak auth subdomain
    # -------------------------------------------------------------------------
    AUTH_CERT_READY="False"
    if [[ -n "$AUTH_HOSTNAME" ]]; then
        log_info "Issuing TLS certificate for Keycloak auth subdomain: ${AUTH_HOSTNAME}..."

        # Clean up previous auth cert attempts
        kubectl delete certificate "${KC_TLS_SECRET}" -n "${OSMO_NS}" --ignore-not-found 2>/dev/null || true
        kubectl delete secret "${KC_TLS_SECRET}" -n "${OSMO_NS}" --ignore-not-found 2>/dev/null || true

        kubectl apply -f - <<EOF
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: osmo-tls-auth-bootstrap
  namespace: ${OSMO_NS}
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt
    acme.cert-manager.io/http01-edit-in-place: "true"
    nginx.ingress.kubernetes.io/ssl-redirect: "false"
spec:
  ingressClassName: nginx
  tls:
    - hosts:
        - ${AUTH_HOSTNAME}
      secretName: ${KC_TLS_SECRET}
  rules:
    - host: ${AUTH_HOSTNAME}
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: osmo-tls-placeholder
                port:
                  number: 80
EOF
        log_success "Auth bootstrap Ingress created"

        if wait_for_certificate "${KC_TLS_SECRET}" 300; then
            AUTH_CERT_READY="True"
        fi

        # Copy auth cert secret to ingress namespace if needed
        copy_secret_across_namespaces "${KC_TLS_SECRET}"

        # Always clean up auth bootstrap Ingress
        log_info "Removing auth bootstrap ingress..."
        kubectl delete ingress osmo-tls-auth-bootstrap -n "${OSMO_NS}" --ignore-not-found 2>/dev/null || true
    fi

    # -------------------------------------------------------------------------
    # Restore OSMO Ingress resources with TLS (Mode B)
    # Restore osmo-ui first so it is oldest for host+path / and wins in NGINX merge (avoids 404 on /).
    # -------------------------------------------------------------------------
    if [[ "$OSMO_DEPLOYED" == "true" && "$CERT_READY" == "True" ]]; then
        log_info "Restoring OSMO Ingress resources with TLS..."

        # Order: osmo-ui first so it is oldest for host osmo.* and wins path / (avoids 404 on /)
        RESTORE_ORDER=()
        for ing_name in "${REMOVED_INGRESSES[@]}"; do
            if [[ "$ing_name" == "osmo-ui" ]]; then
                RESTORE_ORDER=("$ing_name" "${RESTORE_ORDER[@]}")
            else
                RESTORE_ORDER+=("$ing_name")
            fi
        done
        for ing_name in "${RESTORE_ORDER[@]}"; do
            backup_file="/tmp/osmo-tls-backup/${ing_name}.yaml"
            [[ ! -f "$backup_file" ]] && continue

            # Determine which hostname/secret this ingress should use (keycloak = auth host, rest = main host)
            local_host=$(yq -r '.spec.rules[0].host // ""' "$backup_file" 2>/dev/null || \
                         python3 -c "import yaml,sys; d=yaml.safe_load(open('$backup_file')); print(d.get('spec',{}).get('rules',[{}])[0].get('host',''))" 2>/dev/null || echo "")
            tls_secret_name="${TLS_SECRET}"
            tls_host="${MAIN_HOSTNAME}"
            if [[ ( "$ing_name" == "keycloak" || "$local_host" == *"auth"* ) && -n "$AUTH_HOSTNAME" && "$AUTH_CERT_READY" == "True" ]]; then
                tls_secret_name="${KC_TLS_SECRET}"
                tls_host="${AUTH_HOSTNAME}"
            fi

            # Re-apply the backup, then patch host + TLS so Ingress matches intended hostname (not stale .local from backup)
            kubectl apply -f "$backup_file" 2>/dev/null || true
            if kubectl patch ingress "$ing_name" -n "${OSMO_NS}" --type=merge -p '{"metadata":{"annotations":{"nginx.ingress.kubernetes.io/ssl-redirect":"true"}},"spec":{"tls":[{"hosts":["'"${tls_host}"'"],"secretName":"'"${tls_secret_name}"'"}]}}' 2>/dev/null; then
                # Patch host on first rule (some Ingress have multiple rules; we only set rules[0].host)
                kubectl patch ingress "$ing_name" -n "${OSMO_NS}" --type=json -p '[{"op":"replace","path":"/spec/rules/0/host","value":"'"${tls_host}"'"}]' 2>/dev/null || true
                # For osmo host only: give non-UI ingresses path /api or /api/... so only osmo-ui has path / (avoids 404 on /)
                if [[ "$tls_host" == "$MAIN_HOSTNAME" && "$ing_name" != "osmo-ui" ]]; then
                    if [[ "$ing_name" == "osmo-service" ]]; then
                        api_path="/api"
                    else
                        api_path="/api/${ing_name#osmo-}"
                    fi
                    kubectl patch ingress "$ing_name" -n "${OSMO_NS}" --type=json -p '[{"op":"replace","path":"/spec/rules/0/http/paths/0/path","value":"'"${api_path}"'"}]' 2>/dev/null && log_info "  ${ing_name}: path set to ${api_path}" || true
                fi
                log_success "  ${ing_name}: restored with TLS (host: ${tls_host})"
            else
                log_warning "  ${ing_name}: failed to restore"
            fi
        done

        # Clean up backups
        rm -rf /tmp/osmo-tls-backup
    elif [[ "$OSMO_DEPLOYED" == "true" && "$CERT_READY" != "True" ]]; then
        # Certificate failed — restore ingresses without TLS so the app still works
        log_warning "Certificate not ready. Restoring Ingress resources without TLS..."
        for ing_name in "${REMOVED_INGRESSES[@]}"; do
            backup_file="/tmp/osmo-tls-backup/${ing_name}.yaml"
            [[ ! -f "$backup_file" ]] && continue
            kubectl apply -f "$backup_file" 2>/dev/null || true
            log_info "  ${ing_name}: restored (no TLS)"
        done
        rm -rf /tmp/osmo-tls-backup
        log_info "Fix the certificate issue and re-run this script."
    fi

    # -------------------------------------------------------------------------
    # Final cleanup: remove any lingering solver pods
    # -------------------------------------------------------------------------
    kubectl delete pods -n "${OSMO_NS}" -l acme.cert-manager.io/http01-solver=true --ignore-not-found 2>/dev/null || true
    kubectl delete pods -n "${INGRESS_NS}" -l acme.cert-manager.io/http01-solver=true --ignore-not-found 2>/dev/null || true

fi  # end TLS_MODE

# =============================================================================
# Update OSMO service_base_url to HTTPS (only if OSMO is already deployed)
# Use a direct DB upsert so reruns do not corrupt SERVICE.service_auth.
# =============================================================================
if [[ "$OSMO_DEPLOYED" == "true" ]]; then
    log_info "Updating OSMO service_base_url to https://${MAIN_HOSTNAME}..."

    _PF_PID=""
    _PF_LOG=""
    cleanup_tls_pf() {
        stop_port_forward "${_PF_PID:-}" "${_PF_LOG:-}"
    }
    trap cleanup_tls_pf EXIT

    if start_osmo_api_session "${OSMO_NS}" 8080 15; then
        _PF_PID=$PORT_FORWARD_PID
        _PF_LOG=$PORT_FORWARD_LOG
        if upsert_osmo_service_base_url_db "${OSMO_NS}" "https://${MAIN_HOSTNAME}"; then
            NEW_URL=$(osmo_curl GET "${OSMO_API_URL}/api/configs/service" 2>/dev/null | jq -r '.service_base_url // ""')
            log_success "service_base_url updated to: ${NEW_URL}"
        else
            log_warning "Could not update service_base_url automatically."
            log_info "Run: ./08-configure-service-url.sh https://${MAIN_HOSTNAME}"
        fi
    else
        log_warning "Could not connect to OSMO API. Update service_base_url manually:"
        log_info "  ./08-configure-service-url.sh https://${MAIN_HOSTNAME}"
    fi
else
    log_info "Skipping service_base_url update (OSMO not deployed yet)"
    log_info "04-deploy-osmo-control-plane.sh will auto-detect TLS and use https://"
fi

# =============================================================================
# Done
# =============================================================================
echo ""
echo "========================================"
log_success "TLS setup complete (${TLS_MODE})"
echo "========================================"
echo ""

if [[ "$OSMO_DEPLOYED" == "true" ]]; then
    echo "OSMO is now accessible at:"
    echo "  https://${MAIN_HOSTNAME}"
    echo "  https://${MAIN_HOSTNAME}/api/version"
    echo ""
    echo "CLI login:"
    echo "  osmo login https://${MAIN_HOSTNAME} --method dev --username admin"
else
    echo "TLS certificate prepared for: ${MAIN_HOSTNAME}"
    if [[ -n "$AUTH_HOSTNAME" ]]; then
        echo "Auth TLS certificate prepared for: ${AUTH_HOSTNAME}"
    fi
    echo ""
    echo "Next steps:"
    if [[ "$TLS_MODE" == "cert-manager" ]]; then
        echo "  1. Wait for certificate(s) to be ready: kubectl get certificate -n ${OSMO_NS}"
    else
        echo "  1. Certificates stored in: ${CERT_DIR}"
        echo "     Renewal: re-run this script before the 90-day expiry"
    fi
    echo "  2. Deploy OSMO: ./05-deploy-osmo-control-plane.sh"
    echo "     (It will auto-detect the TLS cert and create HTTPS Ingress)"
    if [[ -n "$AUTH_HOSTNAME" ]]; then
        echo "  3. Keycloak will be exposed at https://${AUTH_HOSTNAME}"
    fi
fi
echo ""
