#!/bin/bash
#
# Deploy OSMO Service (Control Plane)
# https://nvidia.github.io/OSMO/main/deployment_guide/getting_started/deploy_service.html
#
# Components: API Service, Router, Web UI, Worker, Logger, Agent, Keycloak
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
source "${SCRIPT_DIR}/lib/common.sh"
source "${SCRIPT_DIR}/defaults.sh"

echo ""
echo "========================================"
echo "  OSMO Service Deployment"
echo "========================================"
echo ""

# Check prerequisites
check_kubectl || exit 1
check_helm || exit 1

# -----------------------------------------------------------------------------
# Validate hostname requirements (early, before any deployment work)
# -----------------------------------------------------------------------------
if [[ "${OSMO_TLS_ENABLED:-false}" == "true" && -z "${OSMO_INGRESS_HOSTNAME:-}" ]]; then
    log_error "OSMO_TLS_ENABLED=true requires OSMO_INGRESS_HOSTNAME to be set."
    echo "  TLS certificates are issued for a domain name, not a bare IP."
    echo "  Set your domain: export OSMO_INGRESS_HOSTNAME=osmo.example.com"
    exit 1
fi

if [[ "${DEPLOY_KEYCLOAK:-false}" == "true" && -z "${OSMO_INGRESS_HOSTNAME:-}" && -z "${KEYCLOAK_HOSTNAME:-}" ]]; then
    log_error "DEPLOY_KEYCLOAK=true requires OSMO_INGRESS_HOSTNAME or KEYCLOAK_HOSTNAME to be set."
    echo "  Example (nip.io, no /etc/hosts): OSMO_INGRESS_HOSTNAME=osmo.<LB_IP_DASHED>.nip.io; KEYCLOAK_HOSTNAME is auto-derived as auth-<OSMO_INGRESS_HOSTNAME> if not set."
    echo "  Set your domain: export OSMO_INGRESS_HOSTNAME=osmo.example.com"
    exit 1
fi

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
OSMO_NAMESPACE="${OSMO_NAMESPACE:-osmo}"
# Deploy Keycloak in same namespace as PostgreSQL to simplify DNS resolution
KEYCLOAK_NAMESPACE="${OSMO_NAMESPACE}"
OSMO_DOMAIN="${OSMO_DOMAIN:-osmo.local}"
# Keycloak's shipped realm template uses 5-minute browser access tokens.
# Refresh the oauth2-proxy session before that JWT expires.
OAUTH2_PROXY_COOKIE_REFRESH="${OAUTH2_PROXY_COOKIE_REFRESH:-4m}"

# Keycloak admin password - check for existing secret first to maintain consistency
if [[ -z "${KEYCLOAK_ADMIN_PASSWORD:-}" ]]; then
    # Try to get existing password from secret
    EXISTING_KC_PASS=$(kubectl get secret keycloak-admin-secret -n "${OSMO_NAMESPACE}" -o jsonpath='{.data.password}' 2>/dev/null | base64 -d 2>/dev/null || true)
    if [[ -n "${EXISTING_KC_PASS}" ]]; then
        KEYCLOAK_ADMIN_PASSWORD="${EXISTING_KC_PASS}"
        log_info "Using existing Keycloak admin password from secret"
    else
        KEYCLOAK_ADMIN_PASSWORD="$(openssl rand -base64 12)"
        log_info "Generated new Keycloak admin password"
    fi
fi

# -----------------------------------------------------------------------------
# Get Database Configuration from Terraform (Nebius Managed PostgreSQL)
# -----------------------------------------------------------------------------
log_info "Using Nebius Managed PostgreSQL..."
    log_info "Retrieving database configuration..."

    # Get connection details from Terraform outputs
    POSTGRES_HOST=$(get_tf_output "postgresql.host" "../001-iac" || echo "")
    POSTGRES_PORT=$(get_tf_output "postgresql.port" "../001-iac" || echo "5432")
    POSTGRES_DB=$(get_tf_output "postgresql.database" "../001-iac" || echo "osmo")
    POSTGRES_USER=$(get_tf_output "postgresql.username" "../001-iac" || echo "osmo_admin")
    
    # Get password - try MysteryBox first, then Terraform output, then env vars
    # MysteryBox secret ID is set by secrets-init.sh as TF_VAR_postgresql_mysterybox_secret_id
    # Fall back to reading the secret ID directly from Terraform outputs
    POSTGRES_SECRET_ID="${TF_VAR_postgresql_mysterybox_secret_id:-${OSMO_POSTGRESQL_SECRET_ID:-}}"
    if [[ -z "$POSTGRES_SECRET_ID" ]]; then
        POSTGRES_SECRET_ID=$(get_tf_output "mysterybox_secrets.postgresql_secret_id" "../001-iac" || echo "")
    fi
    
    if [[ -n "$POSTGRES_SECRET_ID" ]]; then
        log_info "Reading PostgreSQL password from MysteryBox (secret: $POSTGRES_SECRET_ID)..."
        POSTGRES_PASSWORD=$(get_mysterybox_secret "$POSTGRES_SECRET_ID" "password" || echo "")
        if [[ -n "$POSTGRES_PASSWORD" ]]; then
            log_success "PostgreSQL password retrieved from MysteryBox"
        else
            log_warning "Failed to read password from MysteryBox"
        fi
    fi
    
    # Fall back to Terraform output (only works if not using MysteryBox)
    if [[ -z "$POSTGRES_PASSWORD" ]]; then
        POSTGRES_PASSWORD=$(get_tf_output "postgresql_password" "../001-iac" || echo "")
    fi
    
    # Fall back to environment variables or prompt
    if [[ -z "$POSTGRES_HOST" || -z "$POSTGRES_PASSWORD" ]]; then
        log_warning "Could not retrieve PostgreSQL configuration automatically"
        log_info "Checking environment variables..."
        
        POSTGRES_HOST=${POSTGRES_HOST:-${OSMO_POSTGRES_HOST:-""}}
        POSTGRES_PASSWORD=${POSTGRES_PASSWORD:-${OSMO_POSTGRESQL_PASSWORD:-${OSMO_POSTGRES_PASSWORD:-""}}}
        
        if [[ -z "$POSTGRES_HOST" ]]; then
            read_prompt_var "PostgreSQL Host" POSTGRES_HOST ""
        fi
        if [[ -z "$POSTGRES_PASSWORD" ]]; then
            read_secret_var "PostgreSQL Password" POSTGRES_PASSWORD
        fi
    fi

log_success "Database: ${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}"

# -----------------------------------------------------------------------------
# Select Nebius Region
# -----------------------------------------------------------------------------
VALID_REGIONS=("eu-north1" "eu-west2" "me-west1")

if [[ -n "${NEBIUS_REGION:-}" ]]; then
    NEBIUS_SELECTED_REGION="$NEBIUS_REGION"
    matched=false
    for r in "${VALID_REGIONS[@]}"; do
        [[ "$r" == "$NEBIUS_SELECTED_REGION" ]] && matched=true && break
    done
    if ! $matched; then
        log_error "Invalid NEBIUS_REGION '${NEBIUS_SELECTED_REGION}'. Valid options: ${VALID_REGIONS[*]}"
        exit 1
    fi
    log_info "Using region from NEBIUS_REGION: ${NEBIUS_SELECTED_REGION}"
else
    echo "Select the Nebius region for storage:"
    echo ""
    _idx=1
    for _r in "${VALID_REGIONS[@]}"; do
        echo "  ${_idx}) ${_r}"
        _idx=$((_idx + 1))
    done
    echo ""
    while true; do
        printf "Enter choice [1-${#VALID_REGIONS[@]}]: "
        read -r choice
        if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#VALID_REGIONS[@]} )); then
            NEBIUS_SELECTED_REGION="${VALID_REGIONS[$((choice - 1))]}"
            break
        fi
        echo "Invalid selection. Please enter a number between 1 and ${#VALID_REGIONS[@]}."
    done
    log_info "Selected region: ${NEBIUS_SELECTED_REGION}"
fi

S3_NEBIUS_ENDPOINT="https://storage.${NEBIUS_SELECTED_REGION}.nebius.cloud"
S3_NEBIUS_ENDPOINT_HTTPS=$(normalize_nebius_storage_endpoint "${S3_NEBIUS_ENDPOINT}")

# -----------------------------------------------------------------------------
# Get Storage Configuration
# -----------------------------------------------------------------------------
log_info "Retrieving storage configuration..."

S3_BUCKET=$(get_tf_output "storage_bucket.name" "../001-iac" || echo "")
S3_ENDPOINT=$(get_tf_output "storage_bucket.endpoint" "../001-iac" || echo "${S3_NEBIUS_ENDPOINT}")
S3_ACCESS_KEY=$(get_tf_output "storage_credentials.access_key_id" "../001-iac" || echo "")
S3_ENDPOINT=$(normalize_nebius_storage_endpoint "${S3_ENDPOINT}")

# Secret access key is stored in MysteryBox (ephemeral, not in Terraform state)
S3_SECRET_REF_ID=$(get_tf_output "storage_secret_reference_id" "../001-iac" || echo "")
S3_SECRET_KEY=""

if [[ -n "$S3_SECRET_REF_ID" ]]; then
    log_info "Retrieving storage secret from MysteryBox..."
    # IAM access key secrets are stored with key "secret" in MysteryBox
    S3_SECRET_KEY=$(get_mysterybox_secret "$S3_SECRET_REF_ID" "secret" || echo "")
    if [[ -n "$S3_SECRET_KEY" ]]; then
        log_success "Storage secret retrieved from MysteryBox"
    else
        log_warning "Could not retrieve storage secret from MysteryBox"
    fi
fi

if [[ -n "$S3_BUCKET" ]]; then
    log_success "Storage: ${S3_BUCKET} @ ${S3_ENDPOINT}"
fi

# -----------------------------------------------------------------------------
# Add Helm Repositories
# -----------------------------------------------------------------------------
log_info "Adding Helm repositories..."
helm repo add osmo https://helm.ngc.nvidia.com/nvidia/osmo --force-update
helm repo add bitnami https://charts.bitnami.com/bitnami --force-update
helm repo update

# -----------------------------------------------------------------------------
# Step 1: Create Namespaces
# -----------------------------------------------------------------------------
log_info "Creating namespace..."
kubectl create namespace "${OSMO_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
# Note: Keycloak is deployed in the same namespace as OSMO (no separate namespace needed)

# -----------------------------------------------------------------------------
# Step 2: Configure PostgreSQL - Verify Connection and Create Databases
# -----------------------------------------------------------------------------
log_info "Verifying PostgreSQL connection..."

    # Delete any existing test/init pods
    kubectl delete pod osmo-db-test -n "${OSMO_NAMESPACE}" --ignore-not-found 2>/dev/null
    kubectl delete pod osmo-db-init -n "${OSMO_NAMESPACE}" --ignore-not-found 2>/dev/null

    # Create a temporary secret with DB credentials
    # NOTE: PGDATABASE must be the bootstrap database ('osmo') for Nebius MSP PostgreSQL
    kubectl create secret generic osmo-db-init-creds \
        --namespace "${OSMO_NAMESPACE}" \
        --from-literal=PGPASSWORD="${POSTGRES_PASSWORD}" \
        --from-literal=PGHOST="${POSTGRES_HOST}" \
        --from-literal=PGPORT="${POSTGRES_PORT}" \
        --from-literal=PGUSER="${POSTGRES_USER}" \
        --from-literal=PGDATABASE="${POSTGRES_DB}" \
        --dry-run=client -o yaml | kubectl apply -f -

    # -----------------------------------------------------------------------------
    # Connection Test - Verify credentials before proceeding
    # -----------------------------------------------------------------------------
    if [[ "${SKIP_POSTGRES_CONNECTION_TEST:-0}" == "1" || "${SKIP_POSTGRES_CONNECTION_TEST}" == "true" ]]; then
        log_warning "Skipping PostgreSQL connection test (SKIP_POSTGRES_CONNECTION_TEST is set). Fix cluster->DB connectivity (e.g. 001-iac / Nebius console), then re-run 04 without this env to verify."
    else
    log_info "Testing PostgreSQL connection (this may take a moment)..."
    
    kubectl run osmo-db-test \
        --namespace "${OSMO_NAMESPACE}" \
        --image=postgres:16-alpine \
        --restart=Never \
        --env="PGPASSWORD=${POSTGRES_PASSWORD}" \
        --env="PGHOST=${POSTGRES_HOST}" \
        --env="PGPORT=${POSTGRES_PORT}" \
        --env="PGUSER=${POSTGRES_USER}" \
        --env="PGDATABASE=${POSTGRES_DB}" \
        --command -- sh -c 'psql -c "SELECT 1" >/dev/null 2>&1 && echo "CONNECTION_OK" || echo "CONNECTION_FAILED"' \
        >/dev/null 2>&1
    
    # Wait for test pod to complete
    test_elapsed=0
    test_status=""
    while [[ $test_elapsed -lt 60 ]]; do
        test_status=$(kubectl get pod osmo-db-test -n "${OSMO_NAMESPACE}" -o jsonpath='{.status.phase}' 2>/dev/null || echo "Pending")
        if [[ "$test_status" == "Succeeded" || "$test_status" == "Failed" ]]; then
            break
        fi
        sleep 2
        test_elapsed=$((test_elapsed + 2))
    done
    
    # Check test result
    test_result=$(kubectl logs osmo-db-test -n "${OSMO_NAMESPACE}" 2>/dev/null || echo "")
    kubectl delete pod osmo-db-test -n "${OSMO_NAMESPACE}" --ignore-not-found >/dev/null 2>&1
    
    if [[ "$test_result" != *"CONNECTION_OK"* ]]; then
        log_error "PostgreSQL connection test failed!"
        echo ""
        echo "Connection details:"
        echo "  Host:     ${POSTGRES_HOST}"
        echo "  Port:     ${POSTGRES_PORT}"
        echo "  Database: ${POSTGRES_DB}"
        echo "  User:     ${POSTGRES_USER}"
        echo "  Password: (from MysteryBox secret ${TF_VAR_postgresql_mysterybox_secret_id:-'not set'})"
        echo ""
        echo "Possible causes:"
        echo "  1. Password mismatch - MysteryBox password doesn't match PostgreSQL"
        echo "     Fix: Update MysteryBox or recreate PostgreSQL cluster"
        echo "  2. Network issue - Cluster cannot reach PostgreSQL"
        echo "  3. PostgreSQL not ready - Wait and retry"
        echo ""
        echo "To debug manually:"
        echo "  kubectl run psql-debug --rm -it --image=postgres:16-alpine -n osmo -- sh"
        echo "  PGPASSWORD='<password>' psql -h ${POSTGRES_HOST} -U ${POSTGRES_USER} -d ${POSTGRES_DB}"
        echo ""
        echo "To skip the test and continue (e.g. while fixing network): SKIP_POSTGRES_CONNECTION_TEST=1 ./04-deploy-osmo-control-plane.sh"
        exit 1
    fi
    
    log_success "PostgreSQL connection verified"
    fi

    # -----------------------------------------------------------------------------
    # Database Creation
    # -----------------------------------------------------------------------------
    if [[ "${DEPLOY_KEYCLOAK:-false}" == "true" ]]; then
        log_info "Creating OSMO and Keycloak databases (if not exist)..."
    else
        log_info "Verifying OSMO database..."
    fi

    # NOTE: Nebius MSP PostgreSQL creates the bootstrap database ('osmo') automatically.
    # The bootstrap user can only connect to this database, not 'postgres'.
    # We connect to 'osmo' and create additional databases from there.
    # Pass DEPLOY_KEYCLOAK to the init pod
    kubectl apply -n "${OSMO_NAMESPACE}" -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: osmo-db-init
spec:
  containers:
    - name: db-init
      image: postgres:16-alpine
      envFrom:
        - secretRef:
            name: osmo-db-init-creds
      command: ["/bin/sh", "-c"]
      args:
        - |
          set -e
          echo "Connecting to PostgreSQL at \$PGHOST:\$PGPORT as \$PGUSER..."
          
          # NOTE: Nebius MSP PostgreSQL only allows the bootstrap user to connect
          # to the bootstrap database (osmo), not the 'postgres' database.
          # We connect to 'osmo' and create additional databases from there.
          
          # Test connection to the osmo database (created by Nebius during bootstrap)
          if ! psql -d "\${PGDATABASE:-osmo}" -c "SELECT 1" >/dev/null 2>&1; then
            echo "ERROR: Cannot connect to PostgreSQL"
            echo "Debug: PGHOST=\$PGHOST, PGPORT=\$PGPORT, PGUSER=\$PGUSER, PGDATABASE=\${PGDATABASE:-osmo}"
            # Try with verbose error
            psql -d "\${PGDATABASE:-osmo}" -c "SELECT 1" 2>&1 || true
            exit 1
          fi
          echo "Connection successful to database '\${PGDATABASE:-osmo}'"
          
          # The 'osmo' database already exists (created by Nebius bootstrap)
          echo "Database 'osmo' exists (created by Nebius MSP bootstrap)"
          
          # Create keycloak database only if Keycloak deployment is enabled
          DEPLOY_KEYCLOAK="${DEPLOY_KEYCLOAK:-false}"
          if [ "\$DEPLOY_KEYCLOAK" = "true" ]; then
            # Note: This requires the user to have CREATEDB privilege
            if psql -d "\${PGDATABASE:-osmo}" -tAc "SELECT 1 FROM pg_database WHERE datname='keycloak'" | grep -q 1; then
              echo "Database 'keycloak' already exists"
            else
              echo "Creating database 'keycloak'..."
              psql -d "\${PGDATABASE:-osmo}" -c "CREATE DATABASE keycloak;" || {
                echo "WARNING: Could not create 'keycloak' database."
                echo "The bootstrap user may not have CREATEDB privilege."
                echo "Keycloak will use a schema in the 'osmo' database instead."
              }
            fi
          fi
          
          # Verify databases exist
          echo ""
          echo "Verifying databases..."
          psql -d "\${PGDATABASE:-osmo}" -c "\l" | grep -E "osmo" || true
          
          echo ""
          echo "SUCCESS: Database initialization complete"
  restartPolicy: Never
EOF

    # Wait for pod to complete (init pods may finish before Ready condition is detected)
    log_info "Running database initialization..."
    
    # Poll for completion - init pods go directly to Completed/Succeeded very quickly
    max_wait=120
    elapsed=0
    pod_status=""
    
    while [[ $elapsed -lt $max_wait ]]; do
        pod_status=$(kubectl get pod osmo-db-init -n "${OSMO_NAMESPACE}" -o jsonpath='{.status.phase}' 2>/dev/null || echo "Pending")
        
        if [[ "$pod_status" == "Succeeded" ]]; then
            break
        elif [[ "$pod_status" == "Failed" ]]; then
            log_error "Database initialization failed. Checking logs..."
            kubectl logs osmo-db-init -n "${OSMO_NAMESPACE}"
            kubectl delete pod osmo-db-init -n "${OSMO_NAMESPACE}" --ignore-not-found
            exit 1
        fi
        
        sleep 2
        elapsed=$((elapsed + 2))
    done
    
    if [[ "$pod_status" != "Succeeded" ]]; then
        log_error "Database initialization timed out (status: $pod_status). Checking logs..."
        kubectl logs osmo-db-init -n "${OSMO_NAMESPACE}" 2>/dev/null || true
        kubectl delete pod osmo-db-init -n "${OSMO_NAMESPACE}" --ignore-not-found
        exit 1
    fi

    # Show logs for verification
    log_info "Database initialization output:"
    kubectl logs osmo-db-init -n "${OSMO_NAMESPACE}"

    # Cleanup
    kubectl delete pod osmo-db-init -n "${OSMO_NAMESPACE}" --ignore-not-found

log_success "Databases verified and ready"

# -----------------------------------------------------------------------------
# Step 3: Create Secrets
# -----------------------------------------------------------------------------
log_info "Creating secrets..."

# keycloak-db-secret is created later in Step 4 when DEPLOY_KEYCLOAK=true (with other Keycloak secrets)

# Create db-secret that OSMO chart expects (passwordSecretName: db-secret, passwordSecretKey: db-password)
kubectl create secret generic db-secret \
    --namespace "${OSMO_NAMESPACE}" \
    --from-literal=db-password="${POSTGRES_PASSWORD}" \
    --dry-run=client -o yaml | kubectl apply -f -

# OIDC secrets (only needed if Keycloak is deployed)
# These are placeholder values that get overwritten with real Keycloak client secrets
if [[ "${DEPLOY_KEYCLOAK:-false}" == "true" ]]; then
    CLIENT_SECRET=$(openssl rand -base64 32)
    COOKIE_SECRET=$(openssl rand -hex 16)
    kubectl create secret generic oidc-secrets \
        --namespace "${OSMO_NAMESPACE}" \
        --from-literal=client_secret="${CLIENT_SECRET}" \
        --dry-run=client -o yaml | kubectl apply -f -
    # oauth2-proxy-secrets for OSMO 6.2+ (replaces oidc-secrets hmac_secret)
    kubectl create secret generic oauth2-proxy-secrets \
        --namespace "${OSMO_NAMESPACE}" \
        --from-literal=client_secret="${CLIENT_SECRET}" \
        --from-literal=cookie_secret="${COOKIE_SECRET}" \
        --dry-run=client -o yaml | kubectl apply -f -
fi

# Storage secret (required for workflow artifact uploads on Nebius)
if [[ -z "$S3_ACCESS_KEY" || -z "$S3_SECRET_KEY" ]]; then
    log_error "Storage credentials are required but could not be retrieved from Terraform / MysteryBox"
    exit 1
fi

sync_osmo_storage_secret "${OSMO_NAMESPACE}" "${SCRIPT_DIR}/../001-iac" || exit 1

# MEK (Master Encryption Key) Configuration
# OSMO expects MEK in JWK (JSON Web Key) format, base64-encoded
# Reference: https://nvidia.github.io/OSMO/main/deployment_guide/getting_started/deploy_service.html
MEK_ID="${MEK_ID:-key1}"
log_info "Configuring MEK (Master Encryption Key)..."

# Try to read MEK from MysteryBox first (set by secrets-init.sh)
# MysteryBox secret ID is set as TF_VAR_mek_mysterybox_secret_id
MEK_SECRET_ID="${TF_VAR_mek_mysterybox_secret_id:-${OSMO_MEK_SECRET_ID:-}}"
MEK_DATA=""

if [[ -n "$MEK_SECRET_ID" ]]; then
    log_info "Reading MEK from MysteryBox (secret: $MEK_SECRET_ID)..."
    MEK_DATA=$(get_mysterybox_secret "$MEK_SECRET_ID" "mek" || echo "")
    if [[ -n "$MEK_DATA" ]]; then
        log_success "MEK retrieved from MysteryBox"
        # MEK from secrets-init.sh is in format: {"currentMek":"key1","meks":{"key1":"<base64-jwk>"}}
        # Extract the key ID and encoded value
        MEK_ID=$(echo "$MEK_DATA" | jq -r '.currentMek // "key1"' 2>/dev/null || echo "key1")
        MEK_ENCODED=$(echo "$MEK_DATA" | jq -r ".meks.${MEK_ID} // empty" 2>/dev/null || echo "")
        
        if [[ -z "$MEK_ENCODED" ]]; then
            log_warning "Could not parse MEK from MysteryBox, will generate new one"
            MEK_DATA=""
        fi
    else
        log_warning "Failed to read MEK from MysteryBox"
    fi
fi

# Generate new MEK if not retrieved from MysteryBox
if [[ -z "$MEK_DATA" || -z "$MEK_ENCODED" ]]; then
    log_info "Generating new MEK in JWK format..."
    MEK_KEY_RAW="$(openssl rand -base64 32 | tr -d '\n')"
    MEK_JWK="{\"k\":\"${MEK_KEY_RAW}\",\"kid\":\"${MEK_ID}\",\"kty\":\"oct\"}"
    MEK_ENCODED="$(echo -n "$MEK_JWK" | base64 | tr -d '\n')"
    log_success "New MEK generated"
fi

# Create MEK ConfigMap (OSMO expects ConfigMap, not Secret)
kubectl apply -n "${OSMO_NAMESPACE}" -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: mek-config
data:
  mek.yaml: |
    currentMek: ${MEK_ID}
    meks:
      ${MEK_ID}: ${MEK_ENCODED}
EOF

# Also create vault-secrets for backward compatibility (some OSMO versions need this)
kubectl create secret generic vault-secrets \
    --namespace "${OSMO_NAMESPACE}" \
    --from-literal=currentMek="${MEK_ID}" \
    --from-literal=vault-secrets.yaml="currentMek: ${MEK_ID}
meks:
  ${MEK_ID}: ${MEK_ENCODED}" \
    --dry-run=client -o yaml | kubectl apply -f -

log_success "MEK secrets created"

# -----------------------------------------------------------------------------
# Step 3.5: Deploy Redis (Required for OSMO rate limiting)
# -----------------------------------------------------------------------------
log_info "Deploying Redis..."

if kubectl get statefulset redis-master -n "${OSMO_NAMESPACE}" &>/dev/null; then
    log_info "Redis already deployed"
else
    helm upgrade --install redis bitnami/redis \
        --namespace "${OSMO_NAMESPACE}" \
        --version 25.3.1 \
        --set architecture=standalone \
        --set auth.enabled=false \
        --set networkPolicy.enabled=false \
        --set master.persistence.size=50Gi \
        --set master.resources.requests.cpu=8 \
        --set master.resources.requests.memory=52820Mi \
        --set master.resources.limits.cpu=8 \
        --set master.resources.limits.memory=52820Mi \
        --set commonConfiguration="aof-load-corrupt-tail-max-size 10000000" \
        --wait --timeout 10m
    
    log_success "Redis deployed"
fi

REDIS_HOST="redis-master.${OSMO_NAMESPACE}.svc.cluster.local"

# -----------------------------------------------------------------------------
# Step 4: Deploy Keycloak (Enable with DEPLOY_KEYCLOAK=true)
# -----------------------------------------------------------------------------
# Keycloak provides authentication for OSMO
# Required for: osmo login, osmo token, backend operator
# Reference: https://nvidia.github.io/OSMO/main/deployment_guide/getting_started/deploy_service.html#step-2-configure-keycloak

# Keycloak service URL (same namespace as OSMO)
KEYCLOAK_HOST="keycloak.${OSMO_NAMESPACE}.svc.cluster.local"
KEYCLOAK_URL="http://${KEYCLOAK_HOST}:80"

# Derive Keycloak external hostname
# Priority: KEYCLOAK_HOSTNAME env var > auto-derive from OSMO_INGRESS_HOSTNAME
if [[ -n "${KEYCLOAK_HOSTNAME:-}" ]]; then
    AUTH_DOMAIN="${KEYCLOAK_HOSTNAME}"
elif [[ -n "${OSMO_INGRESS_HOSTNAME:-}" ]]; then
    AUTH_DOMAIN="auth-${OSMO_INGRESS_HOSTNAME}"
else
    AUTH_DOMAIN="auth-${OSMO_DOMAIN}"
fi
KC_TLS_SECRET="${KEYCLOAK_TLS_SECRET_NAME:-osmo-tls-auth}"

if [[ "${DEPLOY_KEYCLOAK:-false}" == "true" ]]; then
    log_info "Deploying Keycloak for OSMO authentication..."
    log_info "Reference: https://nvidia.github.io/OSMO/main/deployment_guide/getting_started/deploy_service.html#step-2-configure-keycloak"

    # Keycloak database was already created in Step 2 (osmo-db-init pod) when DEPLOY_KEYCLOAK=true

    # -------------------------------------------------------------------------
    # Step 1: Create secrets for Keycloak
    # -------------------------------------------------------------------------
    log_info "Creating Keycloak secrets..."
    
    # Save admin password to secret for future re-runs
    kubectl create secret generic keycloak-admin-secret \
        --namespace "${OSMO_NAMESPACE}" \
        --from-literal=password="${KEYCLOAK_ADMIN_PASSWORD}" \
        --dry-run=client -o yaml | kubectl apply -f -
    
    # Create keycloak-db-secret for external database (per OSMO docs)
    # Uses the managed PostgreSQL credentials
    kubectl create secret generic keycloak-db-secret \
        --namespace "${OSMO_NAMESPACE}" \
        --from-literal=postgres-password="${POSTGRES_PASSWORD}" \
        --dry-run=client -o yaml | kubectl apply -f -
    
    # Nebius SSO: create OIDC client via npc when CLIENT_ID not set (per-cluster), then create secret for IdP
    if [[ "${NEBIUS_SSO_ENABLED:-false}" == "true" ]]; then
        if [[ -z "${NEBIUS_SSO_ISSUER_URL:-}" ]]; then
            log_error "NEBIUS_SSO_ENABLED=true requires NEBIUS_SSO_ISSUER_URL to be set."
            exit 1
        fi
        # Create OIDC client in Nebius IAM when npc available and CLIENT_ID not set
        if [[ -z "${NEBIUS_SSO_CLIENT_ID:-}" && -n "${AUTH_DOMAIN:-}" && -n "${NEBIUS_PROJECT_ID:-}" ]] && command -v npc &>/dev/null; then
            log_info "Creating Nebius IAM OIDC client (per-cluster) via npc..."
            _oidc_redirect_uri="https://${AUTH_DOMAIN}/realms/osmo/broker/nebius-sso/endpoint"
            _oidc_yaml="/tmp/oidc-client-$$.yaml"
            cat > "$_oidc_yaml" << EOF
metadata:
  parent_id: "${NEBIUS_PROJECT_ID}"
  name: "keycloak-osmo"
spec:
  client_authentication_methods:
    - client_secret_basic
  redirect_uris:
    - "${_oidc_redirect_uri}"
  pkce_enabled: true
  scopes:
    - openid
  session_management_enabled: false
  authorization_grant_types:
    - authorization_code
EOF
            _create_out=$(npc iam oidc-client create --file "$_oidc_yaml" --format yaml 2>&1) || true
            rm -f "$_oidc_yaml"
            _client_id=""
            if [[ -n "$_create_out" ]]; then
                # Parse client id (grep returns 1 when no match; avoid set -e exit)
                _client_id=$(echo "$_create_out" | grep -oE 'id:[[:space:]]*[^[:space:]]+' | head -1 | sed 's/id:[[:space:]]*//') || true
                [[ -z "$_client_id" ]] && _client_id=$(echo "$_create_out" | grep -oE 'oidcclient-[a-z0-9]+' | head -1) || true
                [[ -z "$_client_id" ]] && _client_id=$(echo "$_create_out" | grep -oE 'client_id:[[:space:]]*[^[:space:]]+' | head -1 | sed 's/client_id:[[:space:]]*//') || true
                # npc may return JSON
                [[ -z "$_client_id" ]] && _client_id=$(echo "$_create_out" | jq -r '.id // .client_id // empty' 2>/dev/null) || true
            fi
            if [[ -n "$_client_id" ]]; then
                _secret_out=$(npc iam oidc-client generate-client-secret --client-id "$_client_id" --format yaml 2>&1) || true
                _client_secret=""
                if [[ -n "$_secret_out" ]]; then
                    _client_secret=$(echo "$_secret_out" | grep -E 'client_secret|secret:' | head -1 | sed 's/.*:[[:space:]]*//' | tr -d '"' | tr -d "'" | tr -d '\r') || true
                fi
                if [[ -n "$_client_secret" ]]; then
                    export NEBIUS_SSO_CLIENT_ID="$_client_id"
                    export NEBIUS_SSO_CLIENT_SECRET="$_client_secret"
                    log_success "Created Nebius IAM OIDC client: ${NEBIUS_SSO_CLIENT_ID}. Add NEBIUS_SSO_CLIENT_ID=${NEBIUS_SSO_CLIENT_ID} to osmo-deploy.env for next run."
                else
                    log_warning "OIDC client created but could not parse client secret. Run: npc iam oidc-client generate-client-secret --client-id $_client_id --format yaml"
                    export NEBIUS_SSO_CLIENT_ID="$_client_id"
                fi
            else
                # Client may already exist (e.g. from a previous run); look up by name and use it
                if echo "$_create_out" | grep -qiE 'AlreadyExists|already exists'; then
                    log_info "OIDC client 'keycloak-osmo' already exists; looking up existing client id..."
                    _get_out=$(npc iam oidc-client get-by-name --parent-id "${NEBIUS_PROJECT_ID}" --name "keycloak-osmo" --format yaml 2>&1) || true
                    if [[ -n "$_get_out" ]]; then
                        _client_id=$(echo "$_get_out" | grep -oE 'id:[[:space:]]*[^[:space:]]+' | head -1 | sed 's/id:[[:space:]]*//') || true
                        [[ -z "$_client_id" ]] && _client_id=$(echo "$_get_out" | grep -oE 'oidcclient-[a-z0-9]+' | head -1) || true
                        [[ -z "$_client_id" ]] && _client_id=$(echo "$_get_out" | jq -r '.id // .client_id // empty' 2>/dev/null) || true
                    fi
                    if [[ -n "$_client_id" ]]; then
                        export NEBIUS_SSO_CLIENT_ID="$_client_id"
                        _secret_out=$(npc iam oidc-client generate-client-secret --client-id "$_client_id" --format yaml 2>&1) || true
                        _client_secret=""
                        [[ -n "$_secret_out" ]] && _client_secret=$(echo "$_secret_out" | grep -E 'client_secret|secret:' | head -1 | sed 's/.*:[[:space:]]*//' | tr -d '"' | tr -d "'" | tr -d '\r') || true
                        if [[ -n "$_client_secret" ]]; then
                            export NEBIUS_SSO_CLIENT_SECRET="$_client_secret"
                            log_success "Using existing Nebius IAM OIDC client: ${NEBIUS_SSO_CLIENT_ID} (generated new secret). Add to osmo-deploy.env for next run."
                        else
                            log_success "Using existing Nebius IAM OIDC client: ${NEBIUS_SSO_CLIENT_ID}. Ensure NEBIUS_SSO_CLIENT_SECRET or keycloak-nebius-sso-secret is set."
                        fi
                    fi
                fi
                if [[ -z "${NEBIUS_SSO_CLIENT_ID:-}" ]]; then
                    log_warning "npc oidc-client create failed or returned no id. Create manually (applications/osmo/iam-register/README.md) and set NEBIUS_SSO_CLIENT_ID and NEBIUS_SSO_CLIENT_SECRET."
                    if [[ -n "$_create_out" ]]; then
                        echo "  npc output (for debugging):"
                        echo "$_create_out" | sed 's/^/    /'
                    fi
                fi
            fi
        fi
        if [[ -z "${NEBIUS_SSO_CLIENT_ID:-}" ]]; then
            log_error "NEBIUS_SSO_ENABLED=true requires NEBIUS_SSO_CLIENT_ID (create via npc or set in osmo-deploy.env)."
            echo "  Tip: In the same shell, run: source applications/osmo/deploy/example/000-prerequisites/nebius-env-init.sh"
            echo "       Then re-run: ./04-deploy-osmo-control-plane.sh (npc and NEBIUS_PROJECT_ID must be set for auto-create)."
            echo "       Or create the OIDC client manually: applications/osmo/iam-register/README.md"
            exit 1
        fi
        if [[ -n "${NEBIUS_SSO_CLIENT_SECRET:-}" ]]; then
            kubectl create secret generic keycloak-nebius-sso-secret \
                --namespace "${OSMO_NAMESPACE}" \
                --from-literal=client_secret="${NEBIUS_SSO_CLIENT_SECRET}" \
                --dry-run=client -o yaml | kubectl apply -f -
            log_info "Created keycloak-nebius-sso-secret for Nebius SSO IdP"
        elif ! kubectl get secret keycloak-nebius-sso-secret -n "${OSMO_NAMESPACE}" &>/dev/null; then
            log_error "NEBIUS_SSO_ENABLED=true requires NEBIUS_SSO_CLIENT_SECRET or existing secret keycloak-nebius-sso-secret."
            echo "  Tip: set NEBIUS_SSO_CLIENT_SECRET in osmo-deploy.env (see osmo-deploy.env.example) or create the K8s secret, then re-run."
            exit 1
        fi
    fi

    # Google / GitHub / Microsoft SSO (optional): create K8s secrets when env vars set
    if [[ -n "${GOOGLE_SSO_CLIENT_ID:-}" && -n "${GOOGLE_SSO_CLIENT_SECRET:-}" ]]; then
        kubectl create secret generic keycloak-google-secret \
            --namespace "${OSMO_NAMESPACE}" \
            --from-literal=client_id="${GOOGLE_SSO_CLIENT_ID}" \
            --from-literal=client_secret="${GOOGLE_SSO_CLIENT_SECRET}" \
            --dry-run=client -o yaml | kubectl apply -f -
        log_info "Created keycloak-google-secret for Google IdP"
    fi
    if [[ -n "${GITHUB_SSO_CLIENT_ID:-}" && -n "${GITHUB_SSO_CLIENT_SECRET:-}" ]]; then
        kubectl create secret generic keycloak-github-secret \
            --namespace "${OSMO_NAMESPACE}" \
            --from-literal=client_id="${GITHUB_SSO_CLIENT_ID}" \
            --from-literal=client_secret="${GITHUB_SSO_CLIENT_SECRET}" \
            --dry-run=client -o yaml | kubectl apply -f -
        log_info "Created keycloak-github-secret for GitHub IdP"
    fi
    if [[ -n "${MICROSOFT_SSO_CLIENT_ID:-}" && -n "${MICROSOFT_SSO_CLIENT_SECRET:-}" ]]; then
        kubectl create secret generic keycloak-microsoft-secret \
            --namespace "${OSMO_NAMESPACE}" \
            --from-literal=client_id="${MICROSOFT_SSO_CLIENT_ID}" \
            --from-literal=client_secret="${MICROSOFT_SSO_CLIENT_SECRET}" \
            --dry-run=client -o yaml | kubectl apply -f -
        log_info "Created keycloak-microsoft-secret for Microsoft/Azure IdP"
    fi
    
    log_success "Keycloak secrets created"

    # -------------------------------------------------------------------------
    # Step 2: Install Keycloak using Bitnami Helm chart
    # Per OSMO docs: https://nvidia.github.io/OSMO/main/deployment_guide/getting_started/deploy_service.html#install-keycloak-using-bitnami-helm-chart
    # -------------------------------------------------------------------------
    log_info "Installing Keycloak using Bitnami Helm chart..."
    
    # Add Bitnami repo
    helm repo add bitnami https://charts.bitnami.com/bitnami --force-update 2>/dev/null || true
    helm repo update bitnami
    
    # Determine if Keycloak should use external TLS ingress.
    # Triggers when TLS is enabled with an ingress hostname, OR when KEYCLOAK_HOSTNAME is
    # set explicitly (e.g. KEYCLOAK_HOSTNAME-only deployments with no OSMO_INGRESS_HOSTNAME).
    KC_EXTERNAL="false"
    if [[ ("${OSMO_TLS_ENABLED:-false}" == "true" && -n "${OSMO_INGRESS_HOSTNAME:-}") || -n "${KEYCLOAK_HOSTNAME:-}" ]]; then
        # Check TLS secret for auth domain exists
        if kubectl get secret "${KC_TLS_SECRET}" -n "${OSMO_NAMESPACE}" &>/dev/null || \
           kubectl get secret "${KC_TLS_SECRET}" -n "${INGRESS_NAMESPACE:-ingress-nginx}" &>/dev/null; then
            KC_EXTERNAL="true"
            log_info "Keycloak will be exposed externally at: https://${AUTH_DOMAIN}"
        else
            # Auto-recover: if local cert files exist for the auth domain, recreate the secret
            KC_CERT_DIR="${OSMO_TLS_CERT_DIR:-$HOME/.osmo-certs}"
            KC_LOCAL_CERT="${KC_CERT_DIR}/live/${AUTH_DOMAIN}/fullchain.pem"
            KC_LOCAL_KEY="${KC_CERT_DIR}/live/${AUTH_DOMAIN}/privkey.pem"
            if [[ -f "$KC_LOCAL_CERT" && -f "$KC_LOCAL_KEY" ]]; then
                log_warning "TLS secret '${KC_TLS_SECRET}' for Keycloak not found, but local certs exist."
                log_info "Auto-recovering: recreating secret from ${KC_CERT_DIR}/live/${AUTH_DOMAIN}/..."
                kubectl create secret tls "${KC_TLS_SECRET}" \
                    --cert="${KC_LOCAL_CERT}" \
                    --key="${KC_LOCAL_KEY}" \
                    --namespace "${OSMO_NAMESPACE}" \
                    --dry-run=client -o yaml | kubectl apply -f -
                kubectl create secret tls "${KC_TLS_SECRET}" \
                    --cert="${KC_LOCAL_CERT}" \
                    --key="${KC_LOCAL_KEY}" \
                    --namespace "${INGRESS_NAMESPACE:-ingress-nginx}" \
                    --dry-run=client -o yaml | kubectl apply -f -
                log_success "TLS secret '${KC_TLS_SECRET}' recreated from local cert files"
                KC_EXTERNAL="true"
                log_info "Keycloak will be exposed externally at: https://${AUTH_DOMAIN}"
            else
                log_warning "TLS secret '${KC_TLS_SECRET}' for Keycloak not found."
                log_warning "Run: OSMO_INGRESS_HOSTNAME=${AUTH_DOMAIN} ./03a-setup-tls-certificate.sh"
                log_warning "Keycloak will be internal-only (port-forward access)"
            fi
        fi
    fi

    # Create keycloak-values.yaml per OSMO documentation
    cat > /tmp/keycloak-values.yaml <<EOF
# Keycloak configuration for OSMO
# Based on: https://nvidia.github.io/OSMO/main/deployment_guide/getting_started/deploy_service.html#install-keycloak-using-bitnami-helm-chart

# Override the default image to use bitnamilegacy (no registry subscription needed)
global:
  security:
    allowInsecureImages: true

image:
  registry: docker.io
  repository: bitnamilegacy/keycloak
  tag: 26.1.1-debian-12-r0

$(if [[ "$KC_EXTERNAL" == "true" ]]; then
cat <<KC_PROD
# Production mode with TLS termination at NGINX ingress (proxy=edge)
# Keycloak itself runs plain HTTP; TLS is handled by the NGINX Ingress controller.
production: true
proxy: edge
proxyHeaders: xforwarded
hostname: ${AUTH_DOMAIN}

# Admin user credentials
auth:
  adminUser: admin
  adminPassword: "${KEYCLOAK_ADMIN_PASSWORD}"

# Ingress (exposed via NGINX Ingress with TLS)
ingress:
  enabled: true
  tls: true
  ingressClassName: nginx
  hostname: ${AUTH_DOMAIN}
  annotations:
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
    nginx.ingress.kubernetes.io/proxy-buffer-size: "16k"
  path: /
  pathType: Prefix
  servicePort: 80
  extraTls:
    - hosts:
        - ${AUTH_DOMAIN}
      secretName: ${KC_TLS_SECRET}

# Autoscaling for production
autoscaling:
  enabled: true
  minReplicas: 2
  maxReplicas: 3
  targetCPU: 80
  targetMemory: 80
KC_PROD
else
cat <<KC_DEV
# Development mode (no external ingress, use port-forward for access)
production: false

# Admin user credentials
auth:
  adminUser: admin
  adminPassword: "${KEYCLOAK_ADMIN_PASSWORD}"

# Disable ingress (use port-forward for access in this setup)
ingress:
  enabled: false

# Single replica for dev/test
replicaCount: 1
KC_DEV
fi)

# Resource allocation
resources:
  requests:
    cpu: "500m"
    memory: "768Mi"
  limits:
    cpu: "2"
    memory: "1Gi"

# External Database configuration (using Nebius Managed PostgreSQL)
postgresql:
  enabled: false
externalDatabase:
  host: "${POSTGRES_HOST}"
  port: ${POSTGRES_PORT}
  user: "${POSTGRES_USER}"
  password: "${POSTGRES_PASSWORD}"
  database: "keycloak"

# Additional environment variables (chart already sets KC_BOOTSTRAP_ADMIN_* from auth, KEYCLOAK_DATABASE_* from externalDatabase)
extraEnvVars:
  - name: KC_BOOTSTRAP_ADMIN_USERNAME
    value: "admin"
  - name: KEYCLOAK_ADMIN
    value: "admin"
  - name: KEYCLOAK_ADMIN_PASSWORD
    value: "${KEYCLOAK_ADMIN_PASSWORD}"
  # Database host/port/name/user (chart sets password from externalDatabase.password)
  - name: KEYCLOAK_DATABASE_HOST
    value: "${POSTGRES_HOST}"
  - name: KEYCLOAK_DATABASE_PORT
    value: "${POSTGRES_PORT}"
  - name: KEYCLOAK_DATABASE_NAME
    value: "keycloak"
  - name: KEYCLOAK_DATABASE_USER
    value: "${POSTGRES_USER}"
  - name: KC_DB
    value: "postgres"
  - name: KC_DB_URL_HOST
    value: "${POSTGRES_HOST}"
  - name: KC_DB_URL_PORT
    value: "${POSTGRES_PORT}"
  - name: KC_DB_URL_DATABASE
    value: "keycloak"
  - name: KC_DB_USERNAME
    value: "${POSTGRES_USER}"
  - name: KC_DB_PASSWORD
    value: "${POSTGRES_PASSWORD}"
  # Hostname settings
$(if [[ "$KC_EXTERNAL" == "true" ]]; then
cat <<KC_HOSTNAME_VARS
  - name: KC_HOSTNAME
    value: "${AUTH_DOMAIN}"
  - name: KC_HOSTNAME_STRICT
    value: "true"
  - name: KC_HOSTNAME_STRICT_HTTPS
    value: "true"
  - name: KC_PROXY
    value: "edge"
KC_HOSTNAME_VARS
else
cat <<KC_DEV_VARS
  - name: KC_HOSTNAME_STRICT
    value: "false"
  - name: KC_HOSTNAME_STRICT_HTTPS
    value: "false"
KC_DEV_VARS
fi)
  - name: KC_HTTP_ENABLED
    value: "true"
  - name: KC_HEALTH_ENABLED
    value: "true"
EOF

    # Remove Keycloak StatefulSet so Helm can recreate it (avoids "conflict with helm" on env/annotations from prior runs)
    if kubectl get statefulset keycloak -n "${OSMO_NAMESPACE}" &>/dev/null; then
        log_info "Removing existing Keycloak StatefulSet so Helm can recreate (realm data is in PostgreSQL)..."
        kubectl delete statefulset keycloak -n "${OSMO_NAMESPACE}" --cascade=foreground --timeout=120s 2>/dev/null || \
        kubectl delete statefulset keycloak -n "${OSMO_NAMESPACE}" --ignore-not-found 2>/dev/null || true
        sleep 5
    fi

    # Install or upgrade Keycloak
    # Note: Don't use --wait as it can hang; we'll check status separately
    helm upgrade --install keycloak bitnami/keycloak \
        --version 24.4.9 \
        --namespace "${OSMO_NAMESPACE}" \
        -f /tmp/keycloak-values.yaml \
        --timeout 10m || {
        log_warning "Helm install returned non-zero, checking pod status..."
    }
    
    rm -f /tmp/keycloak-values.yaml
    log_success "Keycloak Helm release installed"
    
    # Wait for Keycloak to be ready
    log_info "Waiting for Keycloak to be ready (this may take 3-5 minutes)..."
    
    # Wait for the pod to exist first
    for i in {1..30}; do
        if kubectl get pods -n "${OSMO_NAMESPACE}" -l app.kubernetes.io/name=keycloak 2>/dev/null | grep -q keycloak; then
            break
        fi
        echo "  Waiting for Keycloak pod to be created... ($i/30)"
        sleep 5
    done
    
    # Now wait for it to be ready
    kubectl wait --for=condition=Ready pod -l app.kubernetes.io/name=keycloak \
        -n "${OSMO_NAMESPACE}" --timeout=300s || {
        log_warning "Keycloak pod not ready yet, checking logs..."
        kubectl logs -n "${OSMO_NAMESPACE}" -l app.kubernetes.io/name=keycloak --tail=30 || true
    }
    
    # Additional wait for Keycloak to fully initialize
    log_info "Waiting for Keycloak to fully initialize..."
    sleep 30
    
    # Configure Keycloak realm using the official OSMO realm JSON
    # Source: https://nvidia.github.io/OSMO/main/deployment_guide/getting_started/deploy_service.html#post-installation-keycloak-configuration
    # The official sample_osmo_realm.json includes everything needed for OSMO RBAC:
    #   - Roles:    osmo-user, osmo-admin, osmo-backend, grafana-*, dashboard-*
    #   - Groups:   Admin, User, Backend Operator (with proper client-role mappings)
    #   - Clients:  osmo-device (public, device code flow), osmo-browser-flow (confidential, auth code)
    #   - Mappers:  "Create roles claim" protocol mapper on both clients (JWT roles claim)
    #   - Scopes:   Standard OIDC scopes (profile, email, roles, etc.)
    log_info "Configuring Keycloak realm using official OSMO realm JSON..."
    
    # Generate client secret for osmo-browser-flow (confidential client)
    OIDC_CLIENT_SECRET=$(openssl rand -hex 16)

    # Determine the browser-facing OSMO URL for Keycloak redirect URIs.
    OSMO_BROWSER_BASE_URL="${OSMO_INGRESS_BASE_URL:-}"
    if [[ -z "$OSMO_BROWSER_BASE_URL" ]]; then
        OSMO_BROWSER_BASE_URL=$(detect_service_url 2>/dev/null || true)
    fi

    if [[ "$KC_EXTERNAL" == "true" && -n "$OSMO_BROWSER_BASE_URL" ]]; then
        OSMO_BASE_URL="${OSMO_BROWSER_BASE_URL}"
    elif [[ "$KC_EXTERNAL" == "true" && -n "${OSMO_INGRESS_HOSTNAME:-}" ]]; then
        OSMO_BASE_URL="https://${OSMO_INGRESS_HOSTNAME}"
    else
        OSMO_BASE_URL="http://localhost:8080"
    fi
    
    # Upload the official realm JSON as a ConfigMap (so the job can mount it)
    log_info "Creating ConfigMap from sample_osmo_realm.json..."
    kubectl create configmap keycloak-realm-json \
        --namespace "${OSMO_NAMESPACE}" \
        --from-file=realm.json="${SCRIPT_DIR}/sample_osmo_realm.json" \
        --dry-run=client -o yaml | kubectl apply -f -
    
    # Create a job to import the realm and configure a test user
    cat > /tmp/keycloak-config-job.yaml <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: keycloak-osmo-setup
  namespace: ${OSMO_NAMESPACE}
spec:
  ttlSecondsAfterFinished: 300
  backoffLimit: 3
  template:
    spec:
      restartPolicy: OnFailure
      volumes:
      - name: realm-json
        configMap:
          name: keycloak-realm-json
      - name: nebius-sso-secret
        secret:
          secretName: keycloak-nebius-sso-secret
          optional: true
      - name: google-sso-secret
        secret:
          secretName: keycloak-google-secret
          optional: true
      - name: github-sso-secret
        secret:
          secretName: keycloak-github-secret
          optional: true
      - name: microsoft-sso-secret
        secret:
          secretName: keycloak-microsoft-secret
          optional: true
      containers:
      - name: keycloak-setup
        image: curlimages/curl:8.5.0
        env:
        - name: OSMO_BASE_URL
          value: "${OSMO_BASE_URL}"
        - name: NEBIUS_SSO_ENABLED
          value: "${NEBIUS_SSO_ENABLED:-false}"
        - name: NEBIUS_SSO_ISSUER_URL
          value: "${NEBIUS_SSO_ISSUER_URL:-}"
        - name: NEBIUS_SSO_CLIENT_ID
          value: "${NEBIUS_SSO_CLIENT_ID:-}"
        - name: NEBIUS_SSO_GROUP_ATTRIBUTE
          value: "${NEBIUS_SSO_GROUP_ATTRIBUTE:-groups}"
        - name: CREATE_OSMO_TEST_USER
          value: "${CREATE_OSMO_TEST_USER:-}"
        - name: AUTH_DOMAIN
          value: "${AUTH_DOMAIN:-}"
        - name: MICROSOFT_SSO_TENANT
          value: "${MICROSOFT_SSO_TENANT:-common}"
        volumeMounts:
        - name: realm-json
          mountPath: /data
          readOnly: true
        - name: nebius-sso-secret
          mountPath: /etc/nebius-sso
          readOnly: true
        - name: google-sso-secret
          mountPath: /etc/keycloak-google
          readOnly: true
        - name: github-sso-secret
          mountPath: /etc/keycloak-github
          readOnly: true
        - name: microsoft-sso-secret
          mountPath: /etc/keycloak-microsoft
          readOnly: true
        command:
        - /bin/sh
        - -c
        - |
          set -e
          KEYCLOAK_URL="http://keycloak:80"

          echo "============================================"
          echo "  OSMO Keycloak Realm Import"
          echo "============================================"
          echo ""

          # ── Step 1: Prepare realm JSON ──────────────────────────
          echo "=== Step 1: Prepare realm JSON ==="
          echo "Customising sample_osmo_realm.json for this deployment..."
          cp /data/realm.json /tmp/realm-import.json

          # Replace placeholder URLs (https://default.com) with actual OSMO URL (from env)
          sed -i "s|https://default.com|\$OSMO_BASE_URL|g" /tmp/realm-import.json

          # Replace masked client secret with generated secret (host-interpolated)
          sed -i 's/"secret": "[*][*]*"/"secret": "${OIDC_CLIENT_SECRET}"/' /tmp/realm-import.json

          echo "  OSMO URL:       \$OSMO_BASE_URL"
          echo "  Realm JSON:     \$(wc -c < /tmp/realm-import.json) bytes"
          echo ""

          # ── Step 2: Wait for Keycloak ───────────────────────────
          echo "=== Step 2: Wait for Keycloak ==="
          for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
            if curl -s -f "\${KEYCLOAK_URL}/health/ready" > /dev/null 2>&1; then
              echo "Keycloak is ready"
              break
            fi
            echo "  Attempt \$i: Keycloak not ready yet..."
            sleep 15
          done
          echo ""
          
          # ── Step 3: Get admin token ─────────────────────────────
          echo "=== Step 3: Get admin token ==="
          for i in 1 2 3 4 5; do
            TOKEN=\$(curl -s -X POST "\${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
              --data-urlencode "client_id=admin-cli" \
              --data-urlencode "username=admin" \
              --data-urlencode "password=${KEYCLOAK_ADMIN_PASSWORD}" \
              --data-urlencode "grant_type=password" | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
            if [ -n "\$TOKEN" ]; then break; fi
            echo "  Retry \$i: waiting for token..."
            sleep 10
          done
          
          if [ -z "\$TOKEN" ]; then
            echo "FATAL: Failed to get admin token"
            exit 1
          fi
          echo "Got admin token"
          echo ""
          
          # ── Step 4: Import OSMO realm ───────────────────────────
          echo "=== Step 4: Import OSMO realm ==="
          
          # Delete existing realm if present (idempotent re-runs)
          REALM_STATUS=\$(curl -s -o /dev/null -w "%{http_code}" "\${KEYCLOAK_URL}/admin/realms/osmo" \
            -H "Authorization: Bearer \$TOKEN")
          if [ "\$REALM_STATUS" = "200" ]; then
            echo "  Existing 'osmo' realm found – deleting for fresh import..."
            curl -s -X DELETE "\${KEYCLOAK_URL}/admin/realms/osmo" \
              -H "Authorization: Bearer \$TOKEN"
            echo "  Old realm deleted"
            sleep 5
          fi
          
          echo "Importing official OSMO realm from sample_osmo_realm.json..."
          IMPORT_HTTP=\$(curl -s -o /tmp/import-resp.txt -w "%{http_code}" \
            -X POST "\${KEYCLOAK_URL}/admin/realms" \
            -H "Authorization: Bearer \$TOKEN" \
            -H "Content-Type: application/json" \
            -d @/tmp/realm-import.json)
          
          if [ "\$IMPORT_HTTP" = "201" ] || [ "\$IMPORT_HTTP" = "204" ]; then
            echo "Realm imported successfully (HTTP \$IMPORT_HTTP)"
          else
            echo "WARNING: Realm import returned HTTP \$IMPORT_HTTP"
            cat /tmp/import-resp.txt 2>/dev/null || true
            echo ""
            # Attempt partial import as fallback
            echo "Trying partial import as fallback..."
            curl -s -X POST "\${KEYCLOAK_URL}/admin/realms/osmo/partialImport" \
              -H "Authorization: Bearer \$TOKEN" \
              -H "Content-Type: application/json" \
              -d @/tmp/realm-import.json || echo "Partial import also failed"
          fi
          
          # Verify realm exists
          sleep 3
          VERIFY=\$(curl -s -o /dev/null -w "%{http_code}" "\${KEYCLOAK_URL}/admin/realms/osmo" \
            -H "Authorization: Bearer \$TOKEN")
          if [ "\$VERIFY" != "200" ]; then
            echo "FATAL: Realm 'osmo' not found after import (HTTP \$VERIFY)"
            exit 1
          fi
          echo "Realm 'osmo' verified"
          echo ""
          
          # ── Step 4b: Set osmo-browser-flow redirect URIs (exact list, no empty string) ───
          # Ensures Keycloak accepts the callback from oauth2-proxy and legacy Envoy paths.
          echo "=== Step 4b: Set osmo-browser-flow redirect URIs ==="
          TOKEN=\$(curl -s -X POST "\${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
            --data-urlencode "client_id=admin-cli" \
            --data-urlencode "username=admin" \
            --data-urlencode "password=${KEYCLOAK_ADMIN_PASSWORD}" \
            --data-urlencode "grant_type=password" | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
          BROWSER_CLIENT_UUID=\$(curl -s "\${KEYCLOAK_URL}/admin/realms/osmo/clients?clientId=osmo-browser-flow" \
            -H "Authorization: Bearer \$TOKEN" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
          if [ -n "\$BROWSER_CLIENT_UUID" ]; then
            REDIRECT_URIS="[\"\$OSMO_BASE_URL/oauth2/callback\",\"\$OSMO_BASE_URL/getAToken\",\"\$OSMO_BASE_URL/api/auth/getAToken\",\"\$OSMO_BASE_URL/setup/getAToken\"]"
            curl -s "\${KEYCLOAK_URL}/admin/realms/osmo/clients/\${BROWSER_CLIENT_UUID}" \
              -H "Authorization: Bearer \$TOKEN" > /tmp/browser-client-redirs.json
            sed -i '/"redirectUris": \[/,/\]/c\
  "redirectUris": '"\$REDIRECT_URIS" /tmp/browser-client-redirs.json
            REDIR_PUT=\$(curl -s -o /dev/null -w "%{http_code}" -X PUT "\${KEYCLOAK_URL}/admin/realms/osmo/clients/\${BROWSER_CLIENT_UUID}" \
              -H "Authorization: Bearer \$TOKEN" \
              -H "Content-Type: application/json" \
              -d @/tmp/browser-client-redirs.json)
            if [ "\$REDIR_PUT" = "204" ] || [ "\$REDIR_PUT" = "200" ]; then
              echo "  Redirect URIs set (oauth2/callback, getAToken, api/auth/getAToken, setup/getAToken)"
            else
              echo "  WARNING: Failed to set redirect URIs (HTTP \$REDIR_PUT)"
            fi
          fi
          echo ""
          
          # ── Step 4c: Set client secret for osmo-browser-flow ───
          # Keycloak ignores the "secret" field during realm import and
          # generates its own random secret. We MUST explicitly set it via the
          # admin API so it matches the oidc-secrets Kubernetes secret that
          # Envoy reads at runtime.
          echo "=== Step 4c: Set osmo-browser-flow client secret ==="
          
          # Refresh token (import may have been slow)
          TOKEN=\$(curl -s -X POST "\${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
            --data-urlencode "client_id=admin-cli" \
            --data-urlencode "username=admin" \
            --data-urlencode "password=${KEYCLOAK_ADMIN_PASSWORD}" \
            --data-urlencode "grant_type=password" | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
          
          # Find the internal UUID for the osmo-browser-flow client
          BROWSER_CLIENT_UUID=\$(curl -s "\${KEYCLOAK_URL}/admin/realms/osmo/clients?clientId=osmo-browser-flow" \
            -H "Authorization: Bearer \$TOKEN" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
          
          if [ -n "\$BROWSER_CLIENT_UUID" ]; then
            echo "  Client UUID: \$BROWSER_CLIENT_UUID"
            
            # GET the full client representation, replace ONLY the secret field, PUT it back.
            # This preserves redirect URIs, scopes, mappers, and all other config.
            curl -s "\${KEYCLOAK_URL}/admin/realms/osmo/clients/\${BROWSER_CLIENT_UUID}" \
              -H "Authorization: Bearer \$TOKEN" > /tmp/browser-client.json
            
            # Replace the masked secret with our generated secret
            # Handle both compact ("secret":"...") and spaced ("secret" : "...") JSON
            sed -i 's/"secret"[ ]*:[ ]*"[^"]*"/"secret":"${OIDC_CLIENT_SECRET}"/' /tmp/browser-client.json
            
            SET_SECRET_HTTP=\$(curl -s -o /dev/null -w "%{http_code}" \
              -X PUT "\${KEYCLOAK_URL}/admin/realms/osmo/clients/\${BROWSER_CLIENT_UUID}" \
              -H "Authorization: Bearer \$TOKEN" \
              -H "Content-Type: application/json" \
              -d @/tmp/browser-client.json)
            
            if [ "\$SET_SECRET_HTTP" = "204" ] || [ "\$SET_SECRET_HTTP" = "200" ]; then
              echo "  Client secret set successfully (HTTP \$SET_SECRET_HTTP)"
            else
              echo "  WARNING: Failed to set client secret (HTTP \$SET_SECRET_HTTP)"
              echo "  OAuth browser flow may fail – check Keycloak logs"
            fi
            
            # Verify: read back the secret and compare
            ACTUAL_SECRET=\$(curl -s "\${KEYCLOAK_URL}/admin/realms/osmo/clients/\${BROWSER_CLIENT_UUID}/client-secret" \
              -H "Authorization: Bearer \$TOKEN" | grep -o '"value":"[^"]*"' | cut -d'"' -f4)
            if [ "\$ACTUAL_SECRET" = "${OIDC_CLIENT_SECRET}" ]; then
              echo "  Verified: client secret matches oidc-secrets"
            else
              echo "  WARNING: Client secret mismatch!"
              echo "  Expected: ${OIDC_CLIENT_SECRET:0:8}..."
              echo "  Got:      \${ACTUAL_SECRET:0:8}..."
              echo "  This will cause 'OAuth flow failed' errors"
            fi
          else
            echo "  WARNING: osmo-browser-flow client not found after import"
            echo "  OAuth browser flow will not work"
          fi
          echo ""
          
          # ── Step 4e: Ensure osmo-device has audience mapper (CLI dev-login token accepted by Envoy) ───
          TOKEN=\$(curl -s -X POST "\${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
            --data-urlencode "client_id=admin-cli" \
            --data-urlencode "username=admin" \
            --data-urlencode "password=${KEYCLOAK_ADMIN_PASSWORD}" \
            --data-urlencode "grant_type=password" | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
          DEVICE_CLIENT_UUID=\$(curl -s "\${KEYCLOAK_URL}/admin/realms/osmo/clients?clientId=osmo-device" \
            -H "Authorization: Bearer \$TOKEN" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
          if [ -n "\$DEVICE_CLIENT_UUID" ]; then
            HAS_AUD=\$(curl -s "\${KEYCLOAK_URL}/admin/realms/osmo/clients/\${DEVICE_CLIENT_UUID}/protocol-mappers/models" \
              -H "Authorization: Bearer \$TOKEN" | grep -o '"name":"audience osmo-device"' || true)
            if [ -z "\$HAS_AUD" ]; then
              AUD_HTTP=\$(curl -s -o /dev/null -w "%{http_code}" -X POST "\${KEYCLOAK_URL}/admin/realms/osmo/clients/\${DEVICE_CLIENT_UUID}/protocol-mappers/models" \
                -H "Authorization: Bearer \$TOKEN" \
                -H "Content-Type: application/json" \
                -d '{"name":"audience osmo-device","protocol":"openid-connect","protocolMapper":"oidc-audience-mapper","consentRequired":false,"config":{"included.custom.audience":"osmo-device","access.token.claim":"true","id.token.claim":"false"}}')
              if [ "\$AUD_HTTP" = "201" ] || [ "\$AUD_HTTP" = "204" ]; then
                echo "  osmo-device audience mapper added (CLI tokens will include aud: osmo-device)"
              fi
            else
              echo "  osmo-device audience mapper already present"
            fi
          fi
          echo ""
          
          # ── Step 4d: Configure Nebius SSO IdP (when enabled) ──────
          if [ "\$NEBIUS_SSO_ENABLED" = "true" ] && [ -n "\$NEBIUS_SSO_ISSUER_URL" ] && [ -n "\$NEBIUS_SSO_CLIENT_ID" ]; then
            echo "=== Step 4c: Configure Nebius SSO Identity Provider ==="
            NEBIUS_SSO_CLIENT_SECRET=""
            if [ -f /etc/nebius-sso/client_secret ]; then
              NEBIUS_SSO_CLIENT_SECRET=\$(cat /etc/nebius-sso/client_secret | tr -d '\n\r')
            fi
            if [ -z "\$NEBIUS_SSO_CLIENT_SECRET" ]; then
              echo "  WARNING: Nebius SSO client secret not found (mount keycloak-nebius-sso-secret) – skipping IdP"
            else
              TOKEN=\$(curl -s -X POST "\${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
                --data-urlencode "client_id=admin-cli" \
                --data-urlencode "username=admin" \
                --data-urlencode "password=${KEYCLOAK_ADMIN_PASSWORD}" \
                --data-urlencode "grant_type=password" | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
              # Escape client secret for JSON (backslash and double-quote)
              SECRET_ESC=\$(echo "\$NEBIUS_SSO_CLIENT_SECRET" | sed 's/\\\\/\\\\\\\\/g; s/"/\\\\"/g')
              # Delete existing nebius-sso IdP if present so re-run 04 always applies current config (e.g. pkceEnabled)
              curl -s -o /dev/null -w "%{http_code}" -X DELETE "\${KEYCLOAK_URL}/admin/realms/osmo/identity-provider/instances/nebius-sso" -H "Authorization: Bearer \$TOKEN" | grep -q 204 && echo "  Removed existing nebius-sso IdP" || true
              # Create custom first-broker-login flow for Nebius SSO:
              # - Review Profile REQUIRED: user sets their username/display name on first login
              # - Handle Existing Account DISABLED: no confusing "link existing account" password form
              # Note: Nebius IAM only returns 'sub' in openid tokens (no preferred_username/email/profile scope)
              #       so the Review Profile step lets users choose a readable display name once.
              curl -s -o /dev/null -X DELETE "\${KEYCLOAK_URL}/admin/realms/osmo/authentication/flows/nebius-first-login" -H "Authorization: Bearer \$TOKEN" || true
              curl -s -o /dev/null -w "%{http_code}" -X POST \
                "\${KEYCLOAK_URL}/admin/realms/osmo/authentication/flows/first%20broker%20login/copy" \
                -H "Authorization: Bearer \$TOKEN" -H "Content-Type: application/json" \
                -d '{"newName":"nebius-first-login"}' | grep -qE "^(2|40)" || true
              # Get the new flow's executions and update requirements
              TOKEN=\$(curl -s -X POST "\${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
                --data-urlencode "client_id=admin-cli" --data-urlencode "username=admin" \
                --data-urlencode "password=${KEYCLOAK_ADMIN_PASSWORD}" --data-urlencode "grant_type=password" \
                | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
              _FLOW_EXECS=\$(curl -s "\${KEYCLOAK_URL}/admin/realms/osmo/authentication/flows/nebius-first-login/executions" -H "Authorization: Bearer \$TOKEN")
              _REVIEW_ID=\$(echo "\$_FLOW_EXECS" | grep -o '"id":"[^"]*","requirement":"[^"]*","displayName":"Review Profile"' | grep -o '"id":"[^"]*"' | cut -d'"' -f4 || echo "\$_FLOW_EXECS" | python3 -c "import json,sys; d=json.load(sys.stdin); [print(e.get('id','')) for e in d if isinstance(e,dict) and 'Review Profile' in e.get('displayName','')]" 2>/dev/null | head -1)
              _HANDLE_ID=\$(echo "\$_FLOW_EXECS" | python3 -c "import json,sys; d=json.load(sys.stdin); [print(e.get('id','')) for e in d if isinstance(e,dict) and 'Handle Existing Account' in e.get('displayName','')]" 2>/dev/null | head -1)
              [ -n "\$_REVIEW_ID" ] && curl -s -o /dev/null -w "%{http_code}" -X PUT \
                "\${KEYCLOAK_URL}/admin/realms/osmo/authentication/flows/nebius-first-login/executions" \
                -H "Authorization: Bearer \$TOKEN" -H "Content-Type: application/json" \
                -d "{\"id\":\"\$_REVIEW_ID\",\"requirement\":\"REQUIRED\"}" | grep -q 204 && echo "  Review Profile: REQUIRED" || true
              [ -n "\$_HANDLE_ID" ] && curl -s -o /dev/null -w "%{http_code}" -X PUT \
                "\${KEYCLOAK_URL}/admin/realms/osmo/authentication/flows/nebius-first-login/executions" \
                -H "Authorization: Bearer \$TOKEN" -H "Content-Type: application/json" \
                -d "{\"id\":\"\$_HANDLE_ID\",\"requirement\":\"DISABLED\"}" | grep -q 204 && echo "  Handle Existing Account: DISABLED" || true
              # OIDC IdP: explicit auth/token URLs (Nebius discovery can yield null); pkceEnabled + pkceMethod S256 required by Nebius SSO
              # authenticateByDefault=false: show login page so users can choose SSO vs local
              echo "{\"alias\":\"nebius-sso\",\"providerId\":\"oidc\",\"displayName\":\"Nebius SSO\",\"enabled\":true,\"authenticateByDefault\":false,\"trustEmail\":true,\"storeToken\":false,\"addReadTokenRoleOnCreate\":false,\"linkOnly\":false,\"firstBrokerLoginFlowAlias\":\"nebius-first-login\",\"config\":{\"issuer\":\"\$NEBIUS_SSO_ISSUER_URL\",\"authorizationUrl\":\"\$NEBIUS_SSO_ISSUER_URL/oauth2/authorize\",\"tokenUrl\":\"\$NEBIUS_SSO_ISSUER_URL/oauth2/token\",\"clientId\":\"\$NEBIUS_SSO_CLIENT_ID\",\"clientSecret\":\"\$SECRET_ESC\",\"clientAuthMethod\":\"client_secret_basic\",\"defaultScope\":\"openid\",\"syncMode\":\"FORCE\",\"useJwksUrl\":\"true\",\"pkceEnabled\":\"true\",\"pkceMethod\":\"S256\"}}" > /tmp/idp.json
              IDP_HTTP=\$(curl -s -o /tmp/idp-resp.txt -w "%{http_code}" -X POST "\${KEYCLOAK_URL}/admin/realms/osmo/identity-provider/instances" \
                -H "Authorization: Bearer \$TOKEN" \
                -H "Content-Type: application/json" \
                -d @/tmp/idp.json)
              if [ "\$IDP_HTTP" = "201" ] || [ "\$IDP_HTTP" = "204" ]; then
                echo "  Nebius SSO IdP created (HTTP \$IDP_HTTP)"
                # Add group mapper: map IdP group attribute to Keycloak realm groups (Admin, User, Backend Operator)
                TOKEN=\$(curl -s -X POST "\${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
                  --data-urlencode "client_id=admin-cli" --data-urlencode "username=admin" \
                  --data-urlencode "password=${KEYCLOAK_ADMIN_PASSWORD}" --data-urlencode "grant_type=password" \
                  | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
                MAPPER_JSON="{\"name\":\"groups\",\"identityProviderMapper\":\"oidc-advanced-group-idp-mapper\",\"identityProviderAlias\":\"nebius-sso\",\"config\":{\"syncMode\":\"INHERIT\",\"groups.claim\":\"\$NEBIUS_SSO_GROUP_ATTRIBUTE\"}}"
                curl -s -X POST "\${KEYCLOAK_URL}/admin/realms/osmo/identity-provider/instances/nebius-sso/mappers" \
                  -H "Authorization: Bearer \$TOKEN" -H "Content-Type: application/json" -d "\$MAPPER_JSON" > /dev/null || echo "  Note: group mapper may need manual config in Keycloak"
                echo "  Group/role mapping: IdP attribute '\$NEBIUS_SSO_GROUP_ATTRIBUTE' -> realm groups"
                echo "  Username: set by user in Review Profile on first login (Nebius only provides 'sub', no preferred_username)"
              else
                echo "  WARNING: Nebius SSO IdP creation returned HTTP \$IDP_HTTP"
                cat /tmp/idp-resp.txt 2>/dev/null || true
              fi
            fi
            echo ""
          fi

          # ── Step 4d: Ensure new users inherit the User group by default ───
          # Nebius SSO may not return realm-group information. Making /User a
          # default group guarantees new brokered users still receive the
          # osmo-user browser-flow role on first login.
          echo "=== Step 4d: Configure default User group ==="
          TOKEN=\$(curl -s -X POST "\${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
            --data-urlencode "client_id=admin-cli" --data-urlencode "username=admin" \
            --data-urlencode "password=${KEYCLOAK_ADMIN_PASSWORD}" --data-urlencode "grant_type=password" \
            | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
          USER_GROUP_ID=\$(curl -s "\${KEYCLOAK_URL}/admin/realms/osmo/groups?search=User" \
            -H "Authorization: Bearer \$TOKEN" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
          if [ -n "\$USER_GROUP_ID" ]; then
            HAS_DEFAULT_USER_GROUP=\$(curl -s "\${KEYCLOAK_URL}/admin/realms/osmo/default-groups" \
              -H "Authorization: Bearer \$TOKEN" | grep -o "\"id\":\"\$USER_GROUP_ID\"" || true)
            if [ -n "\$HAS_DEFAULT_USER_GROUP" ]; then
              echo "  User group already configured as a default group"
            else
              DEFAULT_GROUP_HTTP=\$(curl -s -o /dev/null -w "%{http_code}" -X PUT \
                "\${KEYCLOAK_URL}/admin/realms/osmo/default-groups/\${USER_GROUP_ID}" \
                -H "Authorization: Bearer \$TOKEN")
              if [ "\$DEFAULT_GROUP_HTTP" = "204" ]; then
                echo "  User group configured as a default group"
              else
                echo "  WARNING: Failed to configure default User group (HTTP \$DEFAULT_GROUP_HTTP)"
              fi
            fi
          else
            echo "  WARNING: Could not find realm group 'User'"
          fi
          echo ""

          # ── Step 4e: Google / GitHub / Microsoft IdPs (when secrets mounted) ─
          add_social_idp() {
            ALIAS=\$1
            PROVIDER=\$2
            DISPLAY=\$3
            SECRET_DIR=\$4
            if [ -f "\${SECRET_DIR}/client_id" ] && [ -f "\${SECRET_DIR}/client_secret" ]; then
              CID=\$(cat "\${SECRET_DIR}/client_id" | tr -d '\n\r')
              SECRET=\$(cat "\${SECRET_DIR}/client_secret" | tr -d '\n\r')
              [ -z "\$CID" ] || [ -z "\$SECRET" ] && return
              TOKEN=\$(curl -s -X POST "\${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
                --data-urlencode "client_id=admin-cli" --data-urlencode "username=admin" \
                --data-urlencode "password=${KEYCLOAK_ADMIN_PASSWORD}" --data-urlencode "grant_type=password" \
                | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
              SECRET_ESC=\$(echo "\$SECRET" | sed 's/\\\\/\\\\\\\\/g; s/"/\\\\"/g')
              curl -s -o /dev/null -w "%{http_code}" -X DELETE "\${KEYCLOAK_URL}/admin/realms/osmo/identity-provider/instances/\${ALIAS}" -H "Authorization: Bearer \$TOKEN" | grep -q 204 && echo "  Removed existing \${ALIAS} IdP" || true
              EXTRA=""
              [ "\$PROVIDER" = "microsoft" ] && EXTRA=",\"tenant\":\"\${MICROSOFT_SSO_TENANT:-common}\""
              BODY="{\"alias\":\"\${ALIAS}\",\"providerId\":\"\${PROVIDER}\",\"displayName\":\"\${DISPLAY}\",\"enabled\":true,\"trustEmail\":true,\"firstBrokerLoginFlowAlias\":\"first broker login\",\"config\":{\"clientId\":\"\${CID}\",\"clientSecret\":\"\${SECRET_ESC}\"\${EXTRA}}}"
              HTTP=\$(curl -s -o /tmp/idp-resp.txt -w "%{http_code}" -X POST "\${KEYCLOAK_URL}/admin/realms/osmo/identity-provider/instances" \
                -H "Authorization: Bearer \$TOKEN" -H "Content-Type: application/json" -d "\$BODY")
              if [ "\$HTTP" = "201" ] || [ "\$HTTP" = "204" ]; then
                echo "  \${DISPLAY} IdP created (HTTP \$HTTP)"
                if [ -n "\$AUTH_DOMAIN" ]; then
                  echo "    Redirect URI: https://\${AUTH_DOMAIN}/realms/osmo/broker/\${ALIAS}/endpoint"
                fi
              else
                echo "  WARNING: \${DISPLAY} IdP returned HTTP \$HTTP"
                cat /tmp/idp-resp.txt 2>/dev/null || true
              fi
            fi
          }
          if [ -d /etc/keycloak-google ] || [ -d /etc/keycloak-github ] || [ -d /etc/keycloak-microsoft ]; then
            echo "=== Step 4e: Social IdPs (Google / GitHub / Microsoft) ==="
            add_social_idp "google"   "google"   "Google"   "/etc/keycloak-google"
            add_social_idp "github"   "github"   "GitHub"   "/etc/keycloak-github"
            add_social_idp "microsoft" "microsoft" "Microsoft" "/etc/keycloak-microsoft"
            echo ""
          fi
          
          # ── Step 5: Create test user (skip when Nebius SSO is primary) ─
          CREATE_USER="false"
          if [ "\$NEBIUS_SSO_ENABLED" != "true" ]; then
            CREATE_USER="true"
          fi
          if [ "\$CREATE_OSMO_TEST_USER" = "true" ]; then
            CREATE_USER="true"
          fi
          if [ "\$CREATE_USER" = "true" ]; then
          echo "=== Step 5: Create test user ==="
          
          # Refresh admin token (import may have taken a while)
          TOKEN=\$(curl -s -X POST "\${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token" \
            --data-urlencode "client_id=admin-cli" \
            --data-urlencode "username=admin" \
            --data-urlencode "password=${KEYCLOAK_ADMIN_PASSWORD}" \
            --data-urlencode "grant_type=password" | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
          
          echo "Creating osmo-admin test user..."
          curl -s -X POST "\${KEYCLOAK_URL}/admin/realms/osmo/users" \
            -H "Authorization: Bearer \$TOKEN" \
            -H "Content-Type: application/json" \
            -d '{
              "username": "osmo-admin",
              "enabled": true,
              "emailVerified": true,
              "firstName": "OSMO",
              "lastName": "Admin",
              "email": "osmo-admin@example.com",
              "credentials": [{"type":"password","value":"osmo-admin","temporary":false}]
            }' || echo "User may already exist"
          echo ""
          
          echo "=== Step 6: Assign user to Admin group ==="
          USER_ID=\$(curl -s "\${KEYCLOAK_URL}/admin/realms/osmo/users?username=osmo-admin" \
            -H "Authorization: Bearer \$TOKEN" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
          if [ -n "\$USER_ID" ]; then
            ADMIN_GROUP_ID=\$(curl -s "\${KEYCLOAK_URL}/admin/realms/osmo/groups?search=Admin" \
              -H "Authorization: Bearer \$TOKEN" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
            if [ -n "\$ADMIN_GROUP_ID" ]; then
              curl -s -X PUT "\${KEYCLOAK_URL}/admin/realms/osmo/users/\${USER_ID}/groups/\${ADMIN_GROUP_ID}" \
                -H "Authorization: Bearer \$TOKEN" -H "Content-Type: application/json" -d '{}' || true
              echo "  User 'osmo-admin' assigned to Admin group"
            fi
          fi
          else
          echo "=== Step 5: Skip test user (Nebius SSO is primary; set CREATE_OSMO_TEST_USER=true for break-glass user) ==="
          fi
          echo ""
          
          # ── Done ────────────────────────────────────────────────
          echo "========================================="
          echo "  Keycloak OSMO Configuration Complete"
          echo "========================================="
          echo ""
          echo "Realm:    osmo (imported from official sample_osmo_realm.json)"
          echo "Clients:  osmo-device        (public, device code + direct access)"
          echo "          osmo-browser-flow   (confidential, authorization code)"
          echo "Groups:   Admin, User, Backend Operator"
          echo "Roles:    osmo-admin, osmo-user, osmo-backend, grafana-*, dashboard-*"
          echo "Mappers:  JWT 'roles' claim configured on both clients"
          if [ "\$NEBIUS_SSO_ENABLED" = "true" ]; then
            echo "IdP:      Nebius SSO (primary); no default local user"
          else
            echo "Test user: osmo-admin / osmo-admin (Admin group)"
          fi
          echo ""
EOF

    # Delete any previous config job
    kubectl delete job keycloak-osmo-setup -n "${KEYCLOAK_NAMESPACE}" --ignore-not-found 2>/dev/null || true
    
    kubectl apply -f /tmp/keycloak-config-job.yaml
    
    log_info "Waiting for Keycloak realm import job..."
    kubectl wait --for=condition=complete job/keycloak-osmo-setup \
        -n "${KEYCLOAK_NAMESPACE}" --timeout=300s || {
        log_warning "Keycloak configuration may have failed, check logs:"
        kubectl logs -n "${KEYCLOAK_NAMESPACE}" -l job-name=keycloak-osmo-setup --tail=50 || true
    }
    
    # Set osmo realm frontendUrl so Nebius SSO redirect_uri uses AUTH_DOMAIN (not .local)
    if [[ "$KC_EXTERNAL" == "true" && -n "${AUTH_DOMAIN:-}" && -n "${KEYCLOAK_ADMIN_PASSWORD:-}" ]]; then
        log_info "Setting Keycloak realm 'osmo' frontendUrl to https://${AUTH_DOMAIN} (for Nebius SSO redirect_uri)..."
        KC_URL="https://${AUTH_DOMAIN}"
        _kc_token=""
        _token_resp="/tmp/osmo-kc-token.json"
        for _i in 1 2 3 4 5; do
            curl -sk -X POST "${KC_URL}/realms/master/protocol/openid-connect/token" \
                --data-urlencode "client_id=admin-cli" \
                --data-urlencode "username=admin" \
                --data-urlencode "password=${KEYCLOAK_ADMIN_PASSWORD}" \
                --data-urlencode "grant_type=password" -o "$_token_resp" 2>/dev/null || true
            if [[ -s "$_token_resp" ]] && jq -e . "$_token_resp" >/dev/null 2>&1; then
                _kc_token=$(jq -r '.access_token // empty' "$_token_resp")
            fi
            [[ -n "$_kc_token" ]] && break
            sleep 5
        done
        rm -f "$_token_resp"
        if [[ -n "$_kc_token" ]]; then
            _realm_json="/tmp/osmo-realm-get.json"
            curl -sk -H "Authorization: Bearer $_kc_token" "${KC_URL}/admin/realms/osmo" -o "$_realm_json" 2>/dev/null || true
            if [[ -s "$_realm_json" ]] && jq -e . "$_realm_json" >/dev/null 2>&1; then
                if jq --arg url "https://${AUTH_DOMAIN}" '.attributes = ((.attributes // {}) | . + {"frontendUrl": $url})' "$_realm_json" > /tmp/osmo-realm-frontend.json 2>/dev/null; then
                    _put_code=$(curl -sk -o /dev/null -w "%{http_code}" -X PUT "${KC_URL}/admin/realms/osmo" \
                        -H "Authorization: Bearer $_kc_token" \
                        -H "Content-Type: application/json" \
                        -d @/tmp/osmo-realm-frontend.json 2>/dev/null)
                    if [[ "$_put_code" == "204" || "$_put_code" == "200" ]]; then
                        log_success "Realm frontendUrl set; Nebius SSO redirect_uri will use https://${AUTH_DOMAIN}"
                    else
                        log_warning "Realm frontendUrl PUT returned HTTP $_put_code"
                    fi
                else
                    log_warning "Could not transform realm JSON for frontendUrl"
                fi
            else
                log_warning "Keycloak realm 'osmo' not available yet (GET returned non-JSON or 404). Set frontendUrl later when Keycloak is ready, or re-run this script after Keycloak is up."
            fi
            rm -f /tmp/osmo-realm-frontend.json "$_realm_json"
        else
            log_warning "Could not get Keycloak admin token (is ${KC_URL} reachable?). Nebius SSO may show Invalid redirect_uri."
        fi
    fi

    # Update Nebius IAM OIDC client redirect URI so this cluster's Keycloak callback is allowed (per-cluster).
    if [[ "${NEBIUS_SSO_ENABLED:-false}" == "true" && -n "${NEBIUS_SSO_CLIENT_ID:-}" && -n "${AUTH_DOMAIN:-}" ]]; then
        _npc_redirect_uri="https://${AUTH_DOMAIN}/realms/osmo/broker/nebius-sso/endpoint"
        if command -v npc &>/dev/null; then
            log_info "Updating Nebius IAM OIDC client redirect URI (per-cluster)..."
            if npc iam oidc-client update "${NEBIUS_SSO_CLIENT_ID}" \
                --redirect-uris "${_npc_redirect_uri}" \
                --authorization-grant-types authorization_code \
                --patch \
                --format yaml &>/dev/null; then
                log_success "Nebius IAM OIDC client redirect URI set to ${_npc_redirect_uri}"
            else
                log_warning "npc oidc-client update failed (run manually): npc iam oidc-client update ${NEBIUS_SSO_CLIENT_ID} --redirect-uris '${_npc_redirect_uri}' --authorization-grant-types authorization_code --patch --format yaml"
            fi
        else
            log_warning "npc not found; update Nebius IAM OIDC client redirect URI manually (see end of script output)."
        fi
    fi

    # Store the client secret for OIDC (used by Envoy sidecar)
    kubectl create secret generic oidc-secrets \
        --namespace "${OSMO_NAMESPACE}" \
        --from-literal=client_secret="${OIDC_CLIENT_SECRET}" \
        --dry-run=client -o yaml | kubectl apply -f -
    # oauth2-proxy-secrets for OSMO 6.2+ (replaces oidc-secrets hmac_secret)
    kubectl create secret generic oauth2-proxy-secrets \
        --namespace "${OSMO_NAMESPACE}" \
        --from-literal=client_secret="${OIDC_CLIENT_SECRET}" \
        --from-literal=cookie_secret="$(openssl rand -hex 16)" \
        --dry-run=client -o yaml | kubectl apply -f -
    
    # Clean up temporary files and ConfigMap
    rm -f /tmp/keycloak-values.yaml /tmp/keycloak-config-job.yaml
    kubectl delete configmap keycloak-realm-json -n "${OSMO_NAMESPACE}" --ignore-not-found 2>/dev/null || true
    
    log_success "Keycloak deployed and configured"
    echo ""
    if [[ "$KC_EXTERNAL" == "true" ]]; then
        echo "Keycloak Access (external):"
        echo "  URL: https://${AUTH_DOMAIN}"
        echo "  Admin console: https://${AUTH_DOMAIN}/admin"
        echo "  Admin: admin / ${KEYCLOAK_ADMIN_PASSWORD}"
        if [[ "${NEBIUS_SSO_ENABLED:-false}" == "true" ]]; then
            echo "  Login: Nebius SSO button on Keycloak login page + local osmo-admin fallback (set CREATE_OSMO_TEST_USER=false to suppress local user)"
        else
            echo "  Test User: osmo-admin / osmo-admin"
        fi
        echo ""
        echo "OSMO Auth Endpoints:"
        echo "  Token: https://${AUTH_DOMAIN}/realms/osmo/protocol/openid-connect/token"
        echo "  Auth:  https://${AUTH_DOMAIN}/realms/osmo/protocol/openid-connect/auth"
        echo ""
        # Enable OSMO auth with Envoy sidecars (production mode)
        AUTH_ENABLED="true"
        KEYCLOAK_EXTERNAL_URL="https://${AUTH_DOMAIN}"
        log_success "OSMO authentication will be ENABLED with Envoy sidecars"
    else
        echo "Keycloak Access (port-forward only):"
        echo "  kubectl port-forward -n ${KEYCLOAK_NAMESPACE} svc/keycloak 8081:80"
        echo "  URL: http://localhost:8081"
        echo "  Admin: admin / ${KEYCLOAK_ADMIN_PASSWORD}"
        if [[ "${NEBIUS_SSO_ENABLED:-false}" == "true" ]]; then
            echo "  Login: Nebius SSO (primary)"
        else
            echo "  Test User: osmo-admin / osmo-admin"
        fi
        echo ""
        echo "OSMO Auth Endpoints (in-cluster):"
        echo "  Token: ${KEYCLOAK_URL}/realms/osmo/protocol/openid-connect/token"
        echo "  Auth:  ${KEYCLOAK_URL}/realms/osmo/protocol/openid-connect/auth"
        echo ""
        # Auth disabled when Keycloak is internal-only (no Envoy, open API)
        AUTH_ENABLED="false"
        KEYCLOAK_EXTERNAL_URL=""
        log_info "Note: OSMO auth disabled (Keycloak is internal-only, no TLS ingress)"
        log_info "To enable auth, set up TLS for the auth subdomain and re-run."
    fi
else
    log_info "Skipping Keycloak (set DEPLOY_KEYCLOAK=true to enable)"
    log_warning "Without Keycloak, 'osmo login' and token creation will not work"
    log_info "Reference: https://nvidia.github.io/OSMO/main/deployment_guide/getting_started/deploy_service.html#step-2-configure-keycloak"
    AUTH_ENABLED="false"
    KEYCLOAK_EXTERNAL_URL=""
fi

# -----------------------------------------------------------------------------
# Step 5: Create OSMO Values File
# -----------------------------------------------------------------------------
log_info "Creating OSMO values file..."

# NGINX Ingress – run 03-deploy-nginx-ingress.sh before this script
# When OSMO_INGRESS_HOSTNAME is empty (default), ingress matches any Host header,
# allowing direct IP-based access. Set it to a real domain for host-based routing.
INGRESS_HOSTNAME="${OSMO_INGRESS_HOSTNAME:-}"
TLS_ENABLED="${OSMO_TLS_ENABLED:-false}"
TLS_SECRET_NAME="${OSMO_TLS_SECRET_NAME:-osmo-tls}"
TLS_MODE="${OSMO_TLS_MODE:-}"

if [[ -n "$INGRESS_HOSTNAME" ]]; then
    log_info "Ingress hostname: ${INGRESS_HOSTNAME}"
else
    log_info "Ingress hostname: (any — IP-based access)"
fi

# TLS validation
if [[ "$TLS_ENABLED" == "true" ]]; then
    log_info "TLS is ENABLED"
    if [[ -z "$INGRESS_HOSTNAME" ]]; then
        log_error "TLS is enabled but OSMO_INGRESS_HOSTNAME is not set."
        echo "  TLS certificates are issued for a domain name, not a bare IP."
        echo "  Set your domain: export OSMO_INGRESS_HOSTNAME=osmo.example.com"
        exit 1
    fi
    # Check that the TLS secret exists (created by 03a or 03c)
    OSMO_NS_CHECK="${OSMO_NAMESPACE:-osmo}"
    INGRESS_NS_CHECK="${INGRESS_NAMESPACE:-ingress-nginx}"
    CERT_DIR="${OSMO_TLS_CERT_DIR:-$HOME/.osmo-certs}"
    TLS_SECRET_FOUND="false"
    if kubectl get secret "${TLS_SECRET_NAME}" -n "${OSMO_NS_CHECK}" &>/dev/null || \
       kubectl get secret "${TLS_SECRET_NAME}" -n "${INGRESS_NS_CHECK}" &>/dev/null; then
        TLS_SECRET_FOUND="true"
    fi

    log_success "TLS secret '${TLS_SECRET_NAME}' found"
else
    log_info "TLS is disabled (HTTP only). Set OSMO_TLS_ENABLED=true to enable."
fi

# Create the values file with proper extraEnv and extraVolumes for each service
# This configures PostgreSQL password via env var and MEK via volume mount
cat > /tmp/osmo_values.yaml <<EOF
# OSMO Service values for Nebius deployment
# https://nvidia.github.io/OSMO/main/deployment_guide/getting_started/deploy_service.html

global:
  osmoImageLocation: nvcr.io/nvidia/osmo
  osmoImageTag: latest
  imagePullPolicy: IfNotPresent

# Service configurations (external - not deploying in chart)
services:
  postgres:
    enabled: false
    serviceName: "${POSTGRES_HOST}"
    port: ${POSTGRES_PORT}
    db: osmo
    user: "${POSTGRES_USER}"
    passwordSecretName: db-secret
    passwordSecretKey: db-password
  
  redis:
    enabled: false
    serviceName: "${REDIS_HOST}"
    port: 6379
    tlsEnabled: false
  
  # API service config
  service:
    scaling:
      minReplicas: 1
      maxReplicas: 1
    ingress:
      enabled: true
      prefix: /
      ingressClass: nginx
      sslEnabled: ${TLS_ENABLED}
      annotations:
        nginx.ingress.kubernetes.io/proxy-buffer-size: "16k"
        nginx.ingress.kubernetes.io/proxy-buffers: "8 16k"
        nginx.ingress.kubernetes.io/proxy-busy-buffers-size: "32k"
        nginx.ingress.kubernetes.io/large-client-header-buffers: "4 16k"
$(if [[ "$TLS_ENABLED" == "true" ]]; then
cat <<TLS_ANNOTATIONS
        nginx.ingress.kubernetes.io/ssl-redirect: "true"
$(if [[ "$TLS_MODE" == "cert-manager" ]]; then echo "        cert-manager.io/cluster-issuer: \"${CLUSTER_ISSUER_NAME:-letsencrypt-prod}\""; fi)
TLS_ANNOTATIONS
fi)
$(if [[ "$TLS_ENABLED" == "true" && -n "$INGRESS_HOSTNAME" ]]; then
cat <<TLS_BLOCK
      tls:
        - hosts:
            - ${INGRESS_HOSTNAME}
          secretName: ${TLS_SECRET_NAME}
TLS_BLOCK
fi)
    # Authentication configuration
$(if [[ "$AUTH_ENABLED" == "true" ]]; then
cat <<AUTH_BLOCK
    auth:
      enabled: true
      device_endpoint: ${KEYCLOAK_EXTERNAL_URL}/realms/osmo/protocol/openid-connect/auth/device
      device_client_id: osmo-device
      browser_endpoint: ${KEYCLOAK_EXTERNAL_URL}/realms/osmo/protocol/openid-connect/auth
      browser_client_id: osmo-browser-flow
      token_endpoint: ${KEYCLOAK_EXTERNAL_URL}/realms/osmo/protocol/openid-connect/token
      logout_endpoint: ${KEYCLOAK_EXTERNAL_URL}/realms/osmo/protocol/openid-connect/logout
AUTH_BLOCK
else
cat <<NOAUTH_BLOCK
    # NOTE: Auth is DISABLED. Set DEPLOY_KEYCLOAK=true with TLS to enable.
    auth:
      enabled: false
NOAUTH_BLOCK
fi)
    # PostgreSQL env vars (chart doesn't inject when postgres.enabled=false)
    extraEnv:
      - name: OSMO_POSTGRES_HOST
        value: "${POSTGRES_HOST}"
      - name: OSMO_POSTGRES_PORT
        value: "${POSTGRES_PORT}"
      - name: OSMO_POSTGRES_USER
        value: "${POSTGRES_USER}"
      - name: OSMO_POSTGRES_DATABASE
        value: "osmo"
      - name: OSMO_POSTGRES_PASSWORD
        valueFrom:
          secretKeyRef:
            name: db-secret
            key: db-password
      # Disable built-in OTEL metrics exporter (no collector at localhost:12345)
      - name: METRICS_OTEL_ENABLE
        value: "false"
      # S3-compatible storage endpoint (Nebius Object Storage)
      - name: AWS_ENDPOINT_URL_S3
        value: ${S3_NEBIUS_ENDPOINT_HTTPS}
      - name: AWS_S3_FORCE_PATH_STYLE
        value: "true"
      - name: AWS_DEFAULT_REGION
        value: ${NEBIUS_SELECTED_REGION}
      - name: AWS_ACCESS_KEY_ID
        valueFrom:
          secretKeyRef:
            name: osmo-storage
            key: access-key-id
      - name: AWS_SECRET_ACCESS_KEY
        valueFrom:
          secretKeyRef:
            name: osmo-storage
            key: secret-access-key
      - name: AWS_EC2_METADATA_DISABLED
        value: "true"
      - name: OSMO_SKIP_DATA_AUTH
        value: "1"
    # MEK volume mount
    extraVolumes:
      - name: vault-secrets
        secret:
          secretName: vault-secrets
    extraVolumeMounts:
      - name: vault-secrets
        mountPath: /home/osmo/vault-agent/secrets
        readOnly: true
  
  # Worker service config
  worker:
    scaling:
      minReplicas: 1
      maxReplicas: 1
    extraEnv:
      - name: OSMO_POSTGRES_HOST
        value: "${POSTGRES_HOST}"
      - name: OSMO_POSTGRES_PORT
        value: "${POSTGRES_PORT}"
      - name: OSMO_POSTGRES_USER
        value: "${POSTGRES_USER}"
      - name: OSMO_POSTGRES_DATABASE
        value: "osmo"
      - name: OSMO_POSTGRES_PASSWORD
        valueFrom:
          secretKeyRef:
            name: db-secret
            key: db-password
      # Disable built-in OTEL metrics exporter (no collector at localhost:12345)
      - name: METRICS_OTEL_ENABLE
        value: "false"
      # S3-compatible storage endpoint (Nebius Object Storage)
      - name: AWS_ENDPOINT_URL_S3
        value: ${S3_NEBIUS_ENDPOINT_HTTPS}
      - name: AWS_S3_FORCE_PATH_STYLE
        value: "true"
      - name: AWS_DEFAULT_REGION
        value: ${NEBIUS_SELECTED_REGION}
      - name: AWS_ACCESS_KEY_ID
        valueFrom:
          secretKeyRef:
            name: osmo-storage
            key: access-key-id
      - name: AWS_SECRET_ACCESS_KEY
        valueFrom:
          secretKeyRef:
            name: osmo-storage
            key: secret-access-key
      - name: AWS_EC2_METADATA_DISABLED
        value: "true"
    extraVolumes:
      - name: vault-secrets
        secret:
          secretName: vault-secrets
    extraVolumeMounts:
      - name: vault-secrets
        mountPath: /home/osmo/vault-agent/secrets
        readOnly: true
  
  # Logger service config
  logger:
    scaling:
      minReplicas: 1
      maxReplicas: 1
    extraEnv:
      - name: OSMO_POSTGRES_HOST
        value: "${POSTGRES_HOST}"
      - name: OSMO_POSTGRES_PORT
        value: "${POSTGRES_PORT}"
      - name: OSMO_POSTGRES_USER
        value: "${POSTGRES_USER}"
      - name: OSMO_POSTGRES_DATABASE
        value: "osmo"
      - name: OSMO_POSTGRES_PASSWORD
        valueFrom:
          secretKeyRef:
            name: db-secret
            key: db-password
      # Disable built-in OTEL metrics exporter (no collector at localhost:12345)
      - name: METRICS_OTEL_ENABLE
        value: "false"
      - name: AWS_ENDPOINT_URL_S3
        value: ${S3_NEBIUS_ENDPOINT_HTTPS}
      - name: AWS_S3_FORCE_PATH_STYLE
        value: "true"
      - name: AWS_DEFAULT_REGION
        value: ${NEBIUS_SELECTED_REGION}
      - name: AWS_ACCESS_KEY_ID
        valueFrom:
          secretKeyRef:
            name: osmo-storage
            key: access-key-id
      - name: AWS_SECRET_ACCESS_KEY
        valueFrom:
          secretKeyRef:
            name: osmo-storage
            key: secret-access-key
      - name: AWS_EC2_METADATA_DISABLED
        value: "true"
    extraVolumes:
      - name: vault-secrets
        secret:
          secretName: vault-secrets
    extraVolumeMounts:
      - name: vault-secrets
        mountPath: /home/osmo/vault-agent/secrets
        readOnly: true
  
  # Agent service config
  agent:
    scaling:
      minReplicas: 1
      maxReplicas: 1
    extraEnv:
      - name: OSMO_POSTGRES_HOST
        value: "${POSTGRES_HOST}"
      - name: OSMO_POSTGRES_PORT
        value: "${POSTGRES_PORT}"
      - name: OSMO_POSTGRES_USER
        value: "${POSTGRES_USER}"
      - name: OSMO_POSTGRES_DATABASE
        value: "osmo"
      - name: OSMO_POSTGRES_PASSWORD
        valueFrom:
          secretKeyRef:
            name: db-secret
            key: db-password
      # Disable built-in OTEL metrics exporter (no collector at localhost:12345)
      - name: METRICS_OTEL_ENABLE
        value: "false"
      - name: AWS_ENDPOINT_URL_S3
        value: ${S3_NEBIUS_ENDPOINT_HTTPS}
      - name: AWS_S3_FORCE_PATH_STYLE
        value: "true"
      - name: AWS_DEFAULT_REGION
        value: ${NEBIUS_SELECTED_REGION}
      - name: AWS_ACCESS_KEY_ID
        valueFrom:
          secretKeyRef:
            name: osmo-storage
            key: access-key-id
      - name: AWS_SECRET_ACCESS_KEY
        valueFrom:
          secretKeyRef:
            name: osmo-storage
            key: secret-access-key
      - name: AWS_EC2_METADATA_DISABLED
        value: "true"
    extraVolumes:
      - name: vault-secrets
        secret:
          secretName: vault-secrets
    extraVolumeMounts:
      - name: vault-secrets
        mountPath: /home/osmo/vault-agent/secrets
        readOnly: true
  
  # Delayed job monitor config
  delayedJobMonitor:
    replicas: 1
    extraEnv:
      - name: OSMO_POSTGRES_HOST
        value: "${POSTGRES_HOST}"
      - name: OSMO_POSTGRES_PORT
        value: "${POSTGRES_PORT}"
      - name: OSMO_POSTGRES_USER
        value: "${POSTGRES_USER}"
      - name: OSMO_POSTGRES_DATABASE
        value: "osmo"
      - name: OSMO_POSTGRES_PASSWORD
        valueFrom:
          secretKeyRef:
            name: db-secret
            key: db-password
      # Disable built-in OTEL metrics exporter (no collector at localhost:12345)
      - name: METRICS_OTEL_ENABLE
        value: "false"
    extraVolumes:
      - name: vault-secrets
        secret:
          secretName: vault-secrets
    extraVolumeMounts:
      - name: vault-secrets
        mountPath: /home/osmo/vault-agent/secrets
        readOnly: true

# Sidecar configurations
$(if [[ "$AUTH_ENABLED" == "true" ]]; then
cat <<ENVOY_ENABLED
sidecars:
  authz:
    enabled: true
    # OSMO 6.2 service chart does not currently propagate services.postgres.passwordSecret*
    # into the authz-sidecar container, so inject the password secret explicitly.
    extraEnv:
      - name: OSMO_POSTGRES_PASSWORD
        valueFrom:
          secretKeyRef:
            name: db-secret
            key: db-password
      - name: PGPASSWORD
        valueFrom:
          secretKeyRef:
            name: db-secret
            key: db-password
      - name: POSTGRES_PASSWORD
        valueFrom:
          secretKeyRef:
            name: db-secret
            key: db-password
  envoy:
    enabled: true

    # IDP configuration for JWKS fetching (Envoy needs this to resolve the idp cluster)
    idp:
      host: ${AUTH_DOMAIN}

    # Paths that bypass authentication entirely
    skipAuthPaths:
      - /api/version
      - /api/auth/login
      - /api/auth/keys
      - /api/auth/refresh_token
      - /api/auth/jwt/refresh_token
      - /api/auth/jwt/access_token
      - /client/version

    service:
      port: 8000
      hostname: ${INGRESS_HOSTNAME}
      address: 127.0.0.1

    # JWT Filter config -- three providers
    jwt:
      user_header: x-osmo-user
      providers:
        # Provider 1: Keycloak device flow (CLI)
        - issuer: ${KEYCLOAK_EXTERNAL_URL}/realms/osmo
          audience: osmo-device
          jwks_uri: ${KEYCLOAK_EXTERNAL_URL}/realms/osmo/protocol/openid-connect/certs
          user_claim: preferred_username
          cluster: idp
        # Provider 2: Keycloak browser flow (Web UI)
        - issuer: ${KEYCLOAK_EXTERNAL_URL}/realms/osmo
          audience: osmo-browser-flow
          jwks_uri: ${KEYCLOAK_EXTERNAL_URL}/realms/osmo/protocol/openid-connect/certs
          user_claim: preferred_username
          cluster: idp
        # Provider 3: OSMO-signed JWTs (service accounts)
        - issuer: osmo
          audience: osmo
          jwks_uri: http://localhost:8000/api/auth/keys
          user_claim: unique_name
          cluster: service

  # OAuth2 Proxy sidecar (replaces oauth2Filter in OSMO 6.2+)
  oauth2Proxy:
    enabled: true
    oidcIssuerUrl: ${KEYCLOAK_EXTERNAL_URL}/realms/osmo
    clientId: osmo-browser-flow
    cookieDomain: ${INGRESS_HOSTNAME}
    cookieSecure: true
    cookieRefresh: ${OAUTH2_PROXY_COOKIE_REFRESH}
    useKubernetesSecrets: true
    secretName: oauth2-proxy-secrets
    clientSecretKey: client_secret
    cookieSecretKey: cookie_secret
    extraArgs:
      # Nebius/Keycloak federation may not mark email_verified=true on the browser-flow ID token.
      # oauth2-proxy otherwise rejects the callback with "email in id_token isn't verified".
      - --insecure-oidc-allow-unverified-email=true
$(if [[ "${NEBIUS_SSO_ENABLED:-false}" == "true" ]]; then printf '%s\n' \
'      # Nebius-brokered users may not have a literal email claim in the Keycloak ID token.' \
'      # oauth2-proxy can safely use preferred_username as the browser identity instead.' \
'      - --oidc-email-claim=preferred_username'; fi)
ENVOY_ENABLED
else
cat <<ENVOY_DISABLED
sidecars:
  authz:
    enabled: false
  envoy:
    enabled: false
    service:
      hostname: ${OSMO_DOMAIN}
ENVOY_DISABLED
fi)
  
  # Disable rate limiting (requires proper Redis config)
  rateLimit:
    enabled: false
  
  # Disable log agent (configured for AWS CloudWatch by default, not available on Nebius)
  logAgent:
    enabled: false
  
  # Disable OTEL (requires proper OTEL backend configuration)
  otel:
    enabled: false
EOF

# -----------------------------------------------------------------------------
# Helper: clean up a failed helm release so upgrade --install can proceed
# -----------------------------------------------------------------------------
cleanup_failed_helm_release() {
    local release="$1"
    local ns="$2"
    local status
    status=$(helm status "$release" -n "$ns" -o json 2>/dev/null | jq -r '.info.status // empty' 2>/dev/null || true)
    if [[ "$status" == "failed" ]]; then
        log_warning "Helm release '$release' is in failed state — uninstalling before re-deploy..."
        helm uninstall "$release" -n "$ns" --wait 2>/dev/null || true
    fi
}

cleanup_helm_managed_configmaps() {
    local ns="$1"
    shift
    local deleted_any=false
    local cm

    for cm in "$@"; do
        [[ -z "$cm" ]] && continue
        if kubectl get configmap "$cm" -n "$ns" >/dev/null 2>&1; then
            if [[ "$deleted_any" == "false" ]]; then
                log_info "Removing Helm-managed ConfigMaps so Helm can recreate them cleanly..."
                deleted_any=true
            fi
            kubectl delete configmap "$cm" -n "$ns" --ignore-not-found >/dev/null 2>&1 || true
        fi
    done
}

# -----------------------------------------------------------------------------
# Step 6: Deploy OSMO Service
# -----------------------------------------------------------------------------
log_info "Deploying OSMO Service..."

cleanup_failed_helm_release "osmo-service" "${OSMO_NAMESPACE}"
cleanup_helm_managed_configmaps "${OSMO_NAMESPACE}" \
    osmo-service-envoy-config \
    osmo-agent-envoy-config \
    osmo-logger-envoy-config

SERVICE_HELM_ARGS=(
    --namespace "${OSMO_NAMESPACE}"
    -f /tmp/osmo_values.yaml
)
[[ -n "$INGRESS_HOSTNAME" ]] && SERVICE_HELM_ARGS+=(--set "services.service.hostname=${INGRESS_HOSTNAME}")

helm upgrade --install osmo-service osmo/service \
    "${SERVICE_HELM_ARGS[@]}" \
    --wait --timeout 10m || {
    log_warning "OSMO Service deployment had issues"
    log_info "Checking pod status..."
    kubectl get pods -n "${OSMO_NAMESPACE}" --no-headers | head -10
}

log_success "OSMO Service deployed"

log_success "OSMO Service Helm deployment complete"

# -----------------------------------------------------------------------------
# Step 7: Deploy Router
# -----------------------------------------------------------------------------
log_info "Deploying OSMO Router..."

# Router requires configFile.enabled=true to mount the mek-config ConfigMap
# It also needs db-secret (not postgres-secret) for the password
kubectl create secret generic db-secret \
    --namespace "${OSMO_NAMESPACE}" \
    --from-literal=db-password="${POSTGRES_PASSWORD}" \
    --dry-run=client -o yaml | kubectl apply -f -

ROUTER_HELM_ARGS=(
    --namespace "${OSMO_NAMESPACE}"
    --set service.type=ClusterIP
    --set services.configFile.enabled=true
    --set "services.postgres.serviceName=${POSTGRES_HOST}"
    --set "services.postgres.port=${POSTGRES_PORT}"
    --set services.postgres.db=osmo
    --set "services.postgres.user=${POSTGRES_USER}"
    --set "services.redis.serviceName=${REDIS_HOST}"
    --set services.redis.port=6379
    --set services.redis.tlsEnabled=false
    --set services.service.ingress.enabled=true
    --set services.service.ingress.ingressClass=nginx
    --set "services.service.ingress.sslEnabled=${TLS_ENABLED}"
    --set services.service.scaling.minReplicas=1
    --set services.service.scaling.maxReplicas=1
    --set sidecars.logAgent.enabled=false
)
[[ -n "$INGRESS_HOSTNAME" ]] && ROUTER_HELM_ARGS+=(--set "services.service.hostname=${INGRESS_HOSTNAME}" --set "global.domain=${INGRESS_HOSTNAME}")

# Envoy sidecar config for Router
if [[ "$AUTH_ENABLED" == "true" ]]; then
    log_info "Enabling Envoy sidecar on Router with Keycloak auth..."
    ROUTER_HELM_ARGS+=(
        --set sidecars.envoy.enabled=true
        --set "sidecars.envoy.idp.host=${AUTH_DOMAIN}"
        --set "sidecars.envoy.skipAuthPaths[0]=/api/router/version"
        --set "sidecars.envoy.service.hostname=${INGRESS_HOSTNAME}"
        # JWT filter
        --set sidecars.envoy.jwt.enabled=true
        --set sidecars.envoy.jwt.user_header=x-osmo-user
        # JWT Provider 1: Keycloak device flow (CLI)
        --set "sidecars.envoy.jwt.providers[0].issuer=${KEYCLOAK_EXTERNAL_URL}/realms/osmo"
        --set "sidecars.envoy.jwt.providers[0].audience=osmo-device"
        --set "sidecars.envoy.jwt.providers[0].jwks_uri=${KEYCLOAK_EXTERNAL_URL}/realms/osmo/protocol/openid-connect/certs"
        --set "sidecars.envoy.jwt.providers[0].user_claim=preferred_username"
        --set "sidecars.envoy.jwt.providers[0].cluster=idp"
        # JWT Provider 2: Keycloak browser flow (Web UI)
        --set "sidecars.envoy.jwt.providers[1].issuer=${KEYCLOAK_EXTERNAL_URL}/realms/osmo"
        --set "sidecars.envoy.jwt.providers[1].audience=osmo-browser-flow"
        --set "sidecars.envoy.jwt.providers[1].jwks_uri=${KEYCLOAK_EXTERNAL_URL}/realms/osmo/protocol/openid-connect/certs"
        --set "sidecars.envoy.jwt.providers[1].user_claim=preferred_username"
        --set "sidecars.envoy.jwt.providers[1].cluster=idp"
        # JWT Provider 3: OSMO-signed JWTs (service accounts)
        --set "sidecars.envoy.jwt.providers[2].issuer=osmo"
        --set "sidecars.envoy.jwt.providers[2].audience=osmo"
        --set "sidecars.envoy.jwt.providers[2].jwks_uri=http://osmo-service/api/auth/keys"
        --set "sidecars.envoy.jwt.providers[2].user_claim=unique_name"
        --set "sidecars.envoy.jwt.providers[2].cluster=osmoauth"
        # osmoauth cluster (Router-specific: points to osmo-service)
        --set sidecars.envoy.osmoauth.enabled=true
        --set sidecars.envoy.osmoauth.port=80
        --set "sidecars.envoy.osmoauth.hostname=${INGRESS_HOSTNAME}"
        --set sidecars.envoy.osmoauth.address=osmo-service
        # OAuth2 Proxy (replaces oauth2Filter in OSMO 6.2+)
        --set sidecars.oauth2Proxy.enabled=true
        --set "sidecars.oauth2Proxy.oidcIssuerUrl=${KEYCLOAK_EXTERNAL_URL}/realms/osmo"
        --set sidecars.oauth2Proxy.clientId=osmo-browser-flow
        --set "sidecars.oauth2Proxy.cookieDomain=${INGRESS_HOSTNAME}"
        --set sidecars.oauth2Proxy.cookieSecure=true
        --set-string "sidecars.oauth2Proxy.cookieRefresh=${OAUTH2_PROXY_COOKIE_REFRESH}"
        --set sidecars.oauth2Proxy.useKubernetesSecrets=true
        --set sidecars.oauth2Proxy.secretName=oauth2-proxy-secrets
        --set sidecars.oauth2Proxy.clientSecretKey=client_secret
        --set sidecars.oauth2Proxy.cookieSecretKey=cookie_secret
        --set "sidecars.oauth2Proxy.extraArgs[0]=--insecure-oidc-allow-unverified-email=true"
    )
    if [[ "${NEBIUS_SSO_ENABLED:-false}" == "true" ]]; then
        ROUTER_HELM_ARGS+=(
            --set "sidecars.oauth2Proxy.extraArgs[1]=--oidc-email-claim=preferred_username"
        )
    fi
else
    ROUTER_HELM_ARGS+=(--set sidecars.envoy.enabled=false)
fi

# Proxy buffer annotations for Router ingress (required for OAuth2 -- JWT cookies make headers large)
if [[ "$AUTH_ENABLED" == "true" ]]; then
    ROUTER_HELM_ARGS+=(
        --set-string "services.service.ingress.annotations.nginx\.ingress\.kubernetes\.io/proxy-buffer-size=16k"
        --set-string "services.service.ingress.annotations.nginx\.ingress\.kubernetes\.io/proxy-buffers-number=4"
    )
fi

# TLS settings for Router ingress
if [[ "$TLS_ENABLED" == "true" && -n "$INGRESS_HOSTNAME" ]]; then
    ROUTER_HELM_ARGS+=(
        --set-string "services.service.ingress.annotations.nginx\.ingress\.kubernetes\.io/ssl-redirect=true"
        --set "services.service.ingress.tls[0].hosts[0]=${INGRESS_HOSTNAME}"
        --set "services.service.ingress.tls[0].secretName=${TLS_SECRET_NAME}"
    )
    if [[ "$TLS_MODE" == "cert-manager" ]]; then
        ROUTER_HELM_ARGS+=(
            --set-string "services.service.ingress.annotations.cert-manager\.io/cluster-issuer=${CLUSTER_ISSUER_NAME:-letsencrypt-prod}"
        )
    fi
fi

cleanup_failed_helm_release "osmo-router" "${OSMO_NAMESPACE}"
cleanup_helm_managed_configmaps "${OSMO_NAMESPACE}" osmo-router-envoy-config

helm upgrade --install osmo-router osmo/router \
    "${ROUTER_HELM_ARGS[@]}" \
    --wait --timeout 5m || log_warning "Router deployment had issues"

log_success "OSMO Router deployed"

# -----------------------------------------------------------------------------
# Step 8: Deploy Web UI (Optional)
# -----------------------------------------------------------------------------
if [[ "${DEPLOY_UI:-true}" == "true" ]]; then
    log_info "Deploying OSMO Web UI..."

    # When TLS is enabled, Next.js builds https://<apiHostname>/api/auth/login server-side.
    # Using the internal cluster service (HTTP :80) with SSL=true causes a TLS handshake error
    # ("packet length too long": HTTPS client hits HTTP port). Use the external ingress hostname
    # so the call goes through nginx with proper TLS termination.
    if [[ "$TLS_ENABLED" == "true" && -n "${INGRESS_HOSTNAME:-}" ]]; then
        _UI_API_HOSTNAME="${INGRESS_HOSTNAME}"
    else
        _UI_API_HOSTNAME="osmo-service.${OSMO_NAMESPACE}.svc.cluster.local:80"
    fi

    UI_HELM_ARGS=(
        --namespace "${OSMO_NAMESPACE}"
        --set services.ui.service.type=ClusterIP
        --set "services.redis.serviceName=${REDIS_HOST}"
        --set services.redis.port=6379
        --set services.redis.tlsEnabled=false
        --set services.ui.ingress.enabled=true
        --set services.ui.ingress.ingressClass=nginx
        --set "services.ui.ingress.sslEnabled=${TLS_ENABLED}"
        --set services.ui.replicas=1
        --set "services.ui.apiHostname=${_UI_API_HOSTNAME}"
        --set sidecars.logAgent.enabled=false
        # NEXT_PUBLIC_OSMO_AUTH_HOSTNAME: used by the UI to build Keycloak auth/token/logout URLs
        # as a fallback when the backend /api/auth/login call is unavailable.
        --set "services.ui.extraEnvs[0].name=NEXT_PUBLIC_OSMO_AUTH_HOSTNAME"
        --set "services.ui.extraEnvs[0].value=${AUTH_DOMAIN:-}"
        # NEXT_PUBLIC_OSMO_SSL_ENABLED: must match whether TLS is terminated at the ingress
        # so the UI builds https:// URLs for Keycloak endpoints correctly.
        --set "services.ui.nextjsSslEnabled=${TLS_ENABLED}"
    )
    [[ -n "$INGRESS_HOSTNAME" ]] && UI_HELM_ARGS+=(--set "services.ui.hostname=${INGRESS_HOSTNAME}" --set "global.domain=${INGRESS_HOSTNAME}")

    # Envoy sidecar config for Web UI
    if [[ "$AUTH_ENABLED" == "true" ]]; then
        log_info "Enabling Envoy sidecar on Web UI with Keycloak auth..."
        UI_HELM_ARGS+=(
            --set sidecars.envoy.enabled=true
            --set "sidecars.envoy.idp.host=${AUTH_DOMAIN}"
            --set "sidecars.envoy.service.hostname=${INGRESS_HOSTNAME}"
            --set sidecars.envoy.service.address=127.0.0.1
            --set sidecars.envoy.service.port=8000
            # JWT filter
            --set sidecars.envoy.jwt.user_header=x-osmo-user
            # JWT Provider 1: Keycloak device flow (CLI)
            --set "sidecars.envoy.jwt.providers[0].issuer=${KEYCLOAK_EXTERNAL_URL}/realms/osmo"
            --set "sidecars.envoy.jwt.providers[0].audience=osmo-device"
            --set "sidecars.envoy.jwt.providers[0].jwks_uri=${KEYCLOAK_EXTERNAL_URL}/realms/osmo/protocol/openid-connect/certs"
            --set "sidecars.envoy.jwt.providers[0].user_claim=preferred_username"
            --set "sidecars.envoy.jwt.providers[0].cluster=idp"
            # JWT Provider 2: Keycloak browser flow (Web UI)
            --set "sidecars.envoy.jwt.providers[1].issuer=${KEYCLOAK_EXTERNAL_URL}/realms/osmo"
            --set "sidecars.envoy.jwt.providers[1].audience=osmo-browser-flow"
            --set "sidecars.envoy.jwt.providers[1].jwks_uri=${KEYCLOAK_EXTERNAL_URL}/realms/osmo/protocol/openid-connect/certs"
            --set "sidecars.envoy.jwt.providers[1].user_claim=preferred_username"
            --set "sidecars.envoy.jwt.providers[1].cluster=idp"
            # OAuth2 Proxy (replaces oauth2Filter in OSMO 6.2+)
            --set sidecars.oauth2Proxy.enabled=true
            --set "sidecars.oauth2Proxy.oidcIssuerUrl=${KEYCLOAK_EXTERNAL_URL}/realms/osmo"
            --set sidecars.oauth2Proxy.clientId=osmo-browser-flow
            --set "sidecars.oauth2Proxy.cookieDomain=${INGRESS_HOSTNAME}"
            --set sidecars.oauth2Proxy.cookieSecure=true
            --set-string "sidecars.oauth2Proxy.cookieRefresh=${OAUTH2_PROXY_COOKIE_REFRESH}"
            --set sidecars.oauth2Proxy.useKubernetesSecrets=true
            --set sidecars.oauth2Proxy.secretName=oauth2-proxy-secrets
            --set sidecars.oauth2Proxy.clientSecretKey=client_secret
            --set sidecars.oauth2Proxy.cookieSecretKey=cookie_secret
            # Federated logout: clear the oauth2-proxy cookie and terminate the
            # Keycloak browser session so "Sign out" also works in incognito.
            --set "sidecars.oauth2Proxy.oidcEndSessionUrl=${KEYCLOAK_EXTERNAL_URL}/realms/osmo/protocol/openid-connect/logout"
            --set "sidecars.oauth2Proxy.extraArgs[0]=--insecure-oidc-allow-unverified-email=true"
            --set "sidecars.oauth2Proxy.extraArgs[1]=--whitelist-domain=${AUTH_DOMAIN}"
        )
        if [[ "${NEBIUS_SSO_ENABLED:-false}" == "true" ]]; then
            UI_HELM_ARGS+=(
                --set "sidecars.oauth2Proxy.extraArgs[2]=--oidc-email-claim=preferred_username"
            )
        fi
    else
        UI_HELM_ARGS+=(--set sidecars.envoy.enabled=false)
    fi

    # Proxy buffer annotations for Web UI ingress (required for OAuth2 -- JWT cookies make headers large)
    if [[ "$AUTH_ENABLED" == "true" ]]; then
        UI_HELM_ARGS+=(
            --set-string "services.ui.ingress.annotations.nginx\.ingress\.kubernetes\.io/proxy-buffer-size=16k"
            --set-string "services.ui.ingress.annotations.nginx\.ingress\.kubernetes\.io/proxy-buffers-number=4"
        )
    fi

    # TLS settings for Web UI ingress
    if [[ "$TLS_ENABLED" == "true" && -n "$INGRESS_HOSTNAME" ]]; then
        UI_HELM_ARGS+=(
            --set-string "services.ui.ingress.annotations.nginx\.ingress\.kubernetes\.io/ssl-redirect=true"
            --set "services.ui.ingress.tls[0].hosts[0]=${INGRESS_HOSTNAME}"
            --set "services.ui.ingress.tls[0].secretName=${TLS_SECRET_NAME}"
        )
        if [[ "$TLS_MODE" == "cert-manager" ]]; then
            UI_HELM_ARGS+=(
                --set-string "services.ui.ingress.annotations.cert-manager\.io/cluster-issuer=${CLUSTER_ISSUER_NAME:-letsencrypt-prod}"
            )
        fi
    fi

    # Remove osmo-ui-trpc ingress so Helm can recreate it (avoids "conflict with kubectl-patch" on .spec.rules)
    kubectl delete ingress osmo-ui-trpc -n "${OSMO_NAMESPACE}" --ignore-not-found 2>/dev/null || true

    cleanup_failed_helm_release "osmo-ui" "${OSMO_NAMESPACE}"
    cleanup_helm_managed_configmaps "${OSMO_NAMESPACE}" osmo-ui-envoy-config

    helm upgrade --install osmo-ui osmo/web-ui \
        "${UI_HELM_ARGS[@]}" \
        --wait --timeout 5m || log_warning "UI deployment had issues"

    log_success "OSMO Web UI deployed"

    # tRPC routing fix: the Next.js app has a broken built-in rewrite
    #   /api/:path* → https://undefined/api/:path*  (env var was unset at image build time)
    # The actual tRPC handler in Next.js lives at /trpc/[trpc]/route.js (not /api/trpc/).
    # The Helm chart already creates osmo-ui-trpc ingress with /api/trpc → osmo-ui.
    # We just need to add configuration-snippet so nginx rewrites /api/trpc/... → /trpc/...
    # before proxying, so Next.js receives /trpc/... (valid route) not /api/trpc/... (broken rewrite).
    if [[ -n "${INGRESS_HOSTNAME:-}" ]]; then
        # Enable snippet annotations (required for configuration-snippet; v1.9+ also needs annotations-risk-level=Critical)
        _nginx_ns="${INGRESS_NAMESPACE:-ingress-nginx}"
        if kubectl get configmap ingress-nginx-controller -n "$_nginx_ns" &>/dev/null; then
            _snippets_val=$(kubectl get configmap ingress-nginx-controller -n "$_nginx_ns" -o jsonpath='{.data.allow-snippet-annotations}' 2>/dev/null || true)
            _risk_val=$(kubectl get configmap ingress-nginx-controller -n "$_nginx_ns" -o jsonpath='{.data.annotations-risk-level}' 2>/dev/null || true)
            if [[ "$_snippets_val" != "true" || "$_risk_val" != "Critical" ]]; then
                log_info "Enabling allow-snippet-annotations + annotations-risk-level=Critical in nginx ingress controller..."
                kubectl patch configmap ingress-nginx-controller -n "$_nginx_ns" \
                    --type merge -p '{"data":{"allow-snippet-annotations":"true","annotations-risk-level":"Critical"}}' 2>/dev/null && \
                    log_success "nginx ingress: snippet annotations enabled (risk level=Critical)" || \
                    log_warning "Could not patch nginx ingress ConfigMap — snippet rewrite may fail"
                # Restart controller so the admission webhook picks up the new settings
                kubectl rollout restart deployment/ingress-nginx-controller -n "$_nginx_ns" 2>/dev/null || true
                kubectl rollout status deployment/ingress-nginx-controller -n "$_nginx_ns" --timeout=90s 2>/dev/null || true
            else
                log_info "nginx ingress: snippet annotations already enabled"
            fi
        fi
        # Delete the separate rewrite ingress if it exists from a previous run (no longer needed)
        kubectl delete ingress osmo-ui-trpc-rewrite -n "${OSMO_NAMESPACE}" --ignore-not-found 2>/dev/null || true
        # Patch the Helm-created osmo-ui-trpc ingress to add the rewrite snippet
        if kubectl get ingress osmo-ui-trpc -n "${OSMO_NAMESPACE}" &>/dev/null; then
            kubectl patch ingress osmo-ui-trpc -n "${OSMO_NAMESPACE}" --type merge \
                -p '{"metadata":{"annotations":{"nginx.ingress.kubernetes.io/configuration-snippet":"rewrite ^/api/trpc(/?.*)$ /trpc$1 break;\n"}}}' \
                2>/dev/null && log_info "osmo-ui-trpc ingress: added /api/trpc → /trpc rewrite snippet" || \
                log_warning "Could not patch osmo-ui-trpc ingress with rewrite snippet"
        else
            log_warning "osmo-ui-trpc ingress not found — Helm may not have created it yet; re-run script after Helm deploy"
        fi
    fi
    # Longer timeouts for osmo-service ingress: auth round-trip and workflow submit (CLI read timeout ~60s; use 300s so server is not the bottleneck)
    if kubectl get ingress osmo-service -n "${OSMO_NAMESPACE}" &>/dev/null; then
        kubectl patch ingress osmo-service -n "${OSMO_NAMESPACE}" --type=merge -p '{"metadata":{"annotations":{"nginx.ingress.kubernetes.io/proxy-read-timeout":"300","nginx.ingress.kubernetes.io/proxy-send-timeout":"300","nginx.ingress.kubernetes.io/proxy-connect-timeout":"60"}}}' 2>/dev/null && log_info "osmo-service ingress: proxy timeouts set to 300s (auth + workflow submit)" || true
    fi
fi

# -----------------------------------------------------------------------------
# Envoy hostname + Lua roles
# -----------------------------------------------------------------------------
# Required for: correct redirects (no .local), and Nebius SSO (Lua tolerates JWT
# without .roles). Runs after Helm so chart-created ConfigMaps get patched.
# /api/* is served by osmo-service, so service Envoy must have the same roles fix.
if [[ -n "${AUTH_DOMAIN:-}" && -n "${INGRESS_HOSTNAME:-}" ]]; then
    log_info "Applying Envoy hostname and Lua roles fix (redirects + Nebius SSO)..."
    # If osmo-service-envoy-config is missing (chart sometimes doesn't create it), create from running pod.
    if ! kubectl get configmap osmo-service-envoy-config -n "${OSMO_NAMESPACE}" &>/dev/null; then
        _svc_pod=$(kubectl get pods -n "${OSMO_NAMESPACE}" -l app=osmo-service -o jsonpath='{.items[?(@.status.phase=="Running")].metadata.name}' 2>/dev/null | awk '{print $1}')
        if [[ -n "$_svc_pod" ]]; then
            log_info "Creating missing osmo-service-envoy-config from pod $_svc_pod..."
            kubectl exec -n "${OSMO_NAMESPACE}" "$_svc_pod" -c envoy -- cat /var/config/config.yaml 2>/dev/null > /tmp/osmo-service-envoy-config.yaml || true
            if [[ -s /tmp/osmo-service-envoy-config.yaml ]]; then
                kubectl create configmap osmo-service-envoy-config -n "${OSMO_NAMESPACE}" --from-file=config.yaml=/tmp/osmo-service-envoy-config.yaml 2>/dev/null && log_success "Created osmo-service-envoy-config" || true
            fi
            rm -f /tmp/osmo-service-envoy-config.yaml
        else
            log_warning "osmo-service-envoy-config missing and no running osmo-service pod to copy from; /api may 403 until ConfigMap exists."
        fi
    fi
    for _cm in osmo-ui-envoy-config osmo-router-envoy-config osmo-service-envoy-config osmo-agent-envoy-config osmo-logger-envoy-config; do
        if kubectl get configmap "$_cm" -n "${OSMO_NAMESPACE}" -o json 2>/dev/null | jq -e '.data["config.yaml"]' &>/dev/null; then
            _before=$(kubectl get configmap "$_cm" -n "${OSMO_NAMESPACE}" -o json 2>/dev/null | jq -r '.data["config.yaml"]' | grep -oE 'https?://[^"'\'' ]+' | grep -E 'auth-osmo\.local|osmo\.local' | head -3 || true)
            # Lua fix: avoid "table expected, got nil" when JWT has no .roles (e.g. Nebius SSO).
            # Always ensure osmo-user and osmo-admin are present.
            _lua_safe='local roles = meta.verified_jwt.roles
                          if roles == nil then
                            local ra = meta.verified_jwt.realm_access
                            if ra ~= nil and ra.roles ~= nil then
                              roles = ra.roles
                            end
                          end
                          if roles == nil then
                            roles = {"osmo-user","osmo-admin"}
                          else
                            local has_osmo_user = false
                            local has_osmo_admin = false
                            for _, r in ipairs(roles) do
                              if r == "osmo-user" then has_osmo_user = true end
                              if r == "osmo-admin" then has_osmo_admin = true end
                            end
                            if not has_osmo_user then table.insert(roles, "osmo-user") end
                            if not has_osmo_admin then table.insert(roles, "osmo-admin") end
                          end
                          local roles_list = table.concat(roles, '"'"','"'"')
                          local user = request_handle:headers():get("x-osmo-user")
                          if user == nil or user == "" then
                            user = "osmo-admin"
                          end
                          request_handle:headers():replace("x-osmo-user", user)
                          request_handle:headers():replace("x-osmo-roles", roles_list)'
            # When JWT is missing or verification failed, don't return early: set default meta so
            # the roles Lua still runs and sets x-osmo-user/x-osmo-roles (fixes UI 403 spinner).
            _early_return_patch='if (meta == nil or meta.verified_jwt == nil) then meta = { verified_jwt = {} } end'
            # Service Envoy uses only "if (meta.verified_jwt == nil) then return end"; set meta when nil so Lua does not error.
            _early_return_service='if (meta == nil or meta.verified_jwt == nil) then meta = { verified_jwt = {} } end'
            kubectl get configmap "$_cm" -n "${OSMO_NAMESPACE}" -o json | \
                jq --arg auth "${AUTH_DOMAIN}" --arg host "${INGRESS_HOSTNAME}" --arg lua "$_lua_safe" --arg early "$_early_return_patch" --arg early_svc "$_early_return_service" \
                '.data["config.yaml"] |= (
                   gsub("auth-osmo\\.local"; $auth)
                   | gsub("osmo\\.local"; $host)
                   | gsub("if \\(meta == nil or meta\\.verified_jwt == nil\\) then\\s+return\\s+end"; $early)
                   | gsub("if \\(meta\\.verified_jwt == nil\\) then\\s+return\\s+end"; $early_svc)
                   | gsub("local roles_list = table.concat\\([^)]*\\)"; $lua)
                 )' | \
                kubectl apply -f -
            if [[ -n "$_before" ]]; then
                log_info "Patched $_cm: replaced .local hostnames with ${INGRESS_HOSTNAME} / ${AUTH_DOMAIN}"
            fi
        fi
    done
    # Fix prerendered /auth/login_info: baked at build time with "undefined" hostnames.
    # Next.js statically generated this route during docker build (NEXT_PHASE=phase-production-build),
    # so it serves a cached .body file with browser_endpoint="https://undefined/...".
    # Fix: mount a corrected JSON body via ConfigMap, overriding the bad prerendered cache.
    if kubectl get deployment osmo-ui -n "${OSMO_NAMESPACE}" &>/dev/null; then
        log_info "Fixing prerendered auth/login_info cache (hostname was undefined at build time)..."
        _li_scheme="https"; [[ "${TLS_ENABLED:-false}" != "true" ]] && _li_scheme="http"
        printf '%s' "{\"auth_enabled\":true,\"device_endpoint\":\"${_li_scheme}://${AUTH_DOMAIN}/realms/osmo/protocol/openid-connect/auth/device\",\"device_client_id\":\"osmo-device\",\"browser_endpoint\":\"${_li_scheme}://${AUTH_DOMAIN}/realms/osmo/protocol/openid-connect/auth\",\"browser_client_id\":\"osmo-browser-flow\",\"token_endpoint\":\"${_li_scheme}://${AUTH_DOMAIN}/realms/osmo/protocol/openid-connect/token\",\"logout_endpoint\":\"${_li_scheme}://${AUTH_DOMAIN}/realms/osmo/protocol/openid-connect/logout\"}" \
            > /tmp/osmo_login_info.body
        kubectl create configmap osmo-ui-login-info-cache \
            --from-file=login_info.body=/tmp/osmo_login_info.body \
            -n "${OSMO_NAMESPACE}" \
            --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null && \
            log_info "Created/updated osmo-ui-login-info-cache ConfigMap" || \
            log_warning "Could not create osmo-ui-login-info-cache ConfigMap"
        rm -f /tmp/osmo_login_info.body
        _LI_BODY_PATH="/app/ui/standalone_server.runfiles/osmo_workspace+/ui/standalone_server/standalone/osmo_workspace+/ui/.next/server/app/auth/login_info.body"
        _li_existing_vol=$(kubectl get deployment osmo-ui -n "${OSMO_NAMESPACE}" \
            -o jsonpath='{.spec.template.spec.volumes[*].name}' 2>/dev/null | tr ' ' '\n' | grep -w "login-info-cache" || true)
        if [[ -z "$_li_existing_vol" ]]; then
            # Use strategic merge patch (JSON patch with '-' fails when volumeMounts is empty [])
            kubectl patch deployment osmo-ui -n "${OSMO_NAMESPACE}" --type=strategic -p "{
              \"spec\":{\"template\":{\"spec\":{
                \"volumes\":[{\"name\":\"login-info-cache\",\"configMap\":{\"name\":\"osmo-ui-login-info-cache\"}}],
                \"containers\":[{\"name\":\"osmo-ui\",\"volumeMounts\":[{\"name\":\"login-info-cache\",\"mountPath\":\"${_LI_BODY_PATH}\",\"subPath\":\"login_info.body\"}]}]
              }}}}" 2>/dev/null && log_info "osmo-ui: login_info.body volume mount added" || \
                log_warning "Could not patch osmo-ui deployment for login_info.body; username may still show 'undefined'"
        else
            log_info "osmo-ui already has login-info-cache volume (ConfigMap content updated above)"
        fi
    fi
    # Restart deployments that use Envoy so they pick up patched config
    for _d in osmo-ui osmo-router osmo-service osmo-agent osmo-logger; do
        if kubectl get deployment "$_d" -n "${OSMO_NAMESPACE}" &>/dev/null; then
            kubectl rollout restart deployment "$_d" -n "${OSMO_NAMESPACE}" 2>/dev/null || true
        fi
    done
    # Wait for router, service, and UI so next request hits patched config. /api/* goes to osmo-service.
    if kubectl get deployment osmo-router -n "${OSMO_NAMESPACE}" &>/dev/null; then
        log_info "Waiting for osmo-router rollout to pick up Envoy config..."
        kubectl rollout status deployment osmo-router -n "${OSMO_NAMESPACE}" --timeout=120s 2>/dev/null || true
    fi
    if kubectl get deployment osmo-service -n "${OSMO_NAMESPACE}" &>/dev/null; then
        log_info "Waiting for osmo-service rollout to pick up Envoy config..."
        kubectl rollout status deployment osmo-service -n "${OSMO_NAMESPACE}" --timeout=120s 2>/dev/null || true
    fi
    if kubectl get deployment osmo-ui -n "${OSMO_NAMESPACE}" &>/dev/null; then
        log_info "Waiting for osmo-ui rollout to pick up Envoy config..."
        kubectl rollout status deployment osmo-ui -n "${OSMO_NAMESPACE}" --timeout=120s 2>/dev/null || true
    fi
    # Verify roles fix and early-return fix in router and service (fixes 403 / UI spinner; /api/* is served by service)
    _router_cfg=$(kubectl get configmap osmo-router-envoy-config -n "${OSMO_NAMESPACE}" -o jsonpath='{.data.config\.yaml}' 2>/dev/null || true)
    _service_cfg=$(kubectl get configmap osmo-service-envoy-config -n "${OSMO_NAMESPACE}" -o jsonpath='{.data.config\.yaml}' 2>/dev/null || true)
    if echo "$_router_cfg" | grep -q 'verified_jwt = {}'; then
        log_info "Envoy early-return fix verified in router (nil JWT now gets default roles)."
    fi
    if echo "$_service_cfg" | grep -q 'verified_jwt = {}'; then
        log_info "Envoy early-return fix verified in service (/api/* will get x-osmo-user/x-osmo-roles)."
    fi
    if echo "$_router_cfg" | grep -q 'osmo-admin'; then
        log_info "Envoy roles fix verified in osmo-router-envoy-config (x-osmo-roles will include osmo-admin)."
    else
        log_warning "osmo-router-envoy-config may not have the roles fix (no 'osmo-admin' in Lua). If Workflows page keeps spinning with 403, patch the ConfigMap manually or re-run this script."
    fi
    if [[ -n "$_service_cfg" ]] && ! echo "$_service_cfg" | grep -q 'osmo-admin'; then
        log_warning "osmo-service-envoy-config may not have the roles fix. /api/workflow, /api/users, /api/pool go to service; re-run this script to patch."
    fi
else
    if [[ "${DEPLOY_KEYCLOAK:-false}" == "true" ]]; then
        log_warning "Envoy hostname/Lua fix skipped: AUTH_DOMAIN or INGRESS_HOSTNAME not set. Set OSMO_INGRESS_HOSTNAME (e.g. in osmo-deploy.env) and re-run for correct redirects and Nebius SSO."
    fi
fi

# Cleanup temp files
rm -f /tmp/osmo_values.yaml

# -----------------------------------------------------------------------------
# Step 9: Patch Deployments to Add vault-secrets Volume
# -----------------------------------------------------------------------------
# NOTE: The Helm chart's extraVolumes/extraVolumeMounts values don't work reliably.
# We must patch the deployments after Helm creates them to add the vault-secrets volume.
# This is a known workaround - the env vars work via extraEnv, but volumes don't.

log_info "Patching OSMO deployments to add vault-secrets volume mount..."

# Create the JSON patch file
cat > /tmp/vault-patch.json << 'PATCH_EOF'
[
  {"op": "add", "path": "/spec/template/spec/volumes/-", "value": {"name": "vault-secrets", "secret": {"secretName": "vault-secrets"}}},
  {"op": "add", "path": "/spec/template/spec/containers/0/volumeMounts/-", "value": {"name": "vault-secrets", "mountPath": "/home/osmo/vault-agent/secrets", "readOnly": true}}
]
PATCH_EOF

# All OSMO deployments that need the vault-secrets volume for MEK
OSMO_DEPLOYMENTS=(osmo-service osmo-worker osmo-agent osmo-logger osmo-delayed-job-monitor osmo-router)
VAULT_PATCHED_ANY=false

for deploy in "${OSMO_DEPLOYMENTS[@]}"; do
    if kubectl get deployment/$deploy -n "${OSMO_NAMESPACE}" &>/dev/null; then
        # Check if vault-secrets volume already exists
        EXISTING_VOL=$(kubectl get deployment/$deploy -n "${OSMO_NAMESPACE}" \
            -o jsonpath='{.spec.template.spec.volumes[*].name}' 2>/dev/null | grep -w "vault-secrets" || true)
        
        if [[ -z "$EXISTING_VOL" ]]; then
            log_info "  Patching $deploy to add vault-secrets volume..."
            if kubectl patch deployment/$deploy -n "${OSMO_NAMESPACE}" --type=json --patch-file=/tmp/vault-patch.json; then
                log_success "  $deploy patched successfully"
                VAULT_PATCHED_ANY=true
            else
                log_warning "  Failed to patch $deploy"
            fi
        else
            log_info "  $deploy already has vault-secrets volume, skipping"
        fi
    else
        log_info "  $deploy not found, skipping"
    fi
done

# Cleanup patch file
rm -f /tmp/vault-patch.json

# Wait for rollouts only if we patched something (re-run: skip when all already had vault-secrets)
if [[ "$VAULT_PATCHED_ANY" == "true" ]]; then
    log_info "Waiting for deployments to roll out with new configuration..."
    for deploy in "${OSMO_DEPLOYMENTS[@]}"; do
        if kubectl get deployment/$deploy -n "${OSMO_NAMESPACE}" &>/dev/null; then
            kubectl rollout status deployment/$deploy -n "${OSMO_NAMESPACE}" --timeout=300s || \
                log_warning "  Timeout waiting for $deploy rollout. Check: kubectl get pods -n ${OSMO_NAMESPACE} -l app=$deploy"
        fi
    done
else
    log_info "No vault-secrets patches applied (all already have volume); skipping rollout wait."
fi

log_success "All OSMO deployments patched with vault-secrets volume"

# -----------------------------------------------------------------------------
# Step 10: Patch Services for Direct Access (without Envoy)
# -----------------------------------------------------------------------------
# When Envoy sidecar is disabled, services need to target port 8000 directly
# instead of the 'envoy-http' named port which doesn't exist.
# When Envoy IS enabled, the 'envoy-http' targetPort is correct -- skip patching.

if [[ "$AUTH_ENABLED" == "true" ]]; then
    log_info "Envoy sidecar is ENABLED -- skipping targetPort patches (envoy-http is correct)"
else
    log_info "Verifying service ports (Envoy disabled)..."

    OSMO_SERVICES=(osmo-service osmo-router osmo-logger osmo-agent)

    for svc in "${OSMO_SERVICES[@]}"; do
        if kubectl get svc "$svc" -n "${OSMO_NAMESPACE}" &>/dev/null; then
            CURRENT_TARGET=$(kubectl get svc "$svc" -n "${OSMO_NAMESPACE}" \
                -o jsonpath='{.spec.ports[0].targetPort}' 2>/dev/null || echo "")
            
            if [[ "$CURRENT_TARGET" == "envoy-http" || "$CURRENT_TARGET" == "envoy" ]]; then
                log_info "  Patching $svc: targetPort envoy-http -> 8000"
                kubectl patch svc "$svc" -n "${OSMO_NAMESPACE}" --type='json' \
                    -p='[{"op": "replace", "path": "/spec/ports/0/targetPort", "value": 8000}]' || \
                    log_warning "  Failed to patch $svc"
            else
                log_info "  $svc: targetPort = $CURRENT_TARGET (OK)"
            fi
        fi
    done

    log_success "Service ports verified"
fi

# -----------------------------------------------------------------------------
# Step 11: Verify Deployment
# -----------------------------------------------------------------------------
echo ""
log_info "Verifying deployment configuration..."

# Verify vault-secrets volumes are mounted
echo ""
echo "Volume configuration verification:"
for deploy in "${OSMO_DEPLOYMENTS[@]}"; do
    if kubectl get deployment/$deploy -n "${OSMO_NAMESPACE}" &>/dev/null; then
        VOL_CHECK=$(kubectl get deployment/$deploy -n "${OSMO_NAMESPACE}" \
            -o jsonpath='{.spec.template.spec.volumes[*].name}' 2>/dev/null | grep -w "vault-secrets" || echo "")
        ENV_CHECK=$(kubectl get deployment/$deploy -n "${OSMO_NAMESPACE}" \
            -o jsonpath='{.spec.template.spec.containers[0].env[*].name}' 2>/dev/null | grep -w "OSMO_POSTGRES_PASSWORD" || echo "")
        
        VOL_STATUS="✗"
        ENV_STATUS="✗"
        [[ -n "$VOL_CHECK" ]] && VOL_STATUS="✓"
        [[ -n "$ENV_CHECK" ]] && ENV_STATUS="✓"
        
        echo "  $deploy: vault-secrets=$VOL_STATUS, postgres_env=$ENV_STATUS"
    fi
done

echo ""
echo "Pods:"
kubectl get pods -n "${OSMO_NAMESPACE}"

echo ""
echo "Services:"
kubectl get svc -n "${OSMO_NAMESPACE}"

# -----------------------------------------------------------------------------
# Step 11.5: Refresh internal service auth config
# -----------------------------------------------------------------------------
# OSMO stores SERVICE config as key/value rows in Postgres. Rebuild service_auth
# on reruns before touching service_base_url so workflow submission uses a fresh
# signing key.
if [[ "${REFRESH_SERVICE_AUTH_CONFIG_ON_05:-true}" == "true" ]]; then
    echo ""
    log_info "Refreshing internal OSMO service auth config..."

    if delete_osmo_service_auth_db "${OSMO_NAMESPACE}" "${POSTGRES_HOST}" "${POSTGRES_PORT}" "${POSTGRES_DB}" "${POSTGRES_USER}" "${POSTGRES_PASSWORD}"; then
        log_info "Restarting osmo-service so it can regenerate service_auth..."
        kubectl rollout restart deployment osmo-service -n "${OSMO_NAMESPACE}" >/dev/null 2>&1 || true
        kubectl rollout status deployment osmo-service -n "${OSMO_NAMESPACE}" --timeout=180s || log_warning "osmo-service rollout did not complete cleanly"

        if wait_for_condition \
            "SERVICE service_auth regeneration" \
            120 5 \
            osmo_service_config_key_exists "${OSMO_NAMESPACE}" service_auth "${POSTGRES_HOST}" "${POSTGRES_PORT}" "${POSTGRES_DB}" "${POSTGRES_USER}" "${POSTGRES_PASSWORD}"; then
            log_success "SERVICE service_auth regenerated"
        else
            log_warning "SERVICE service_auth was not observed in Postgres after osmo-service restart"
        fi

        for _deploy in osmo-agent osmo-worker osmo-logger; do
            if kubectl get deployment "${_deploy}" -n "${OSMO_NAMESPACE}" &>/dev/null; then
                log_info "Restarting ${_deploy} to pick up fresh service auth config..."
                kubectl rollout restart deployment "${_deploy}" -n "${OSMO_NAMESPACE}" >/dev/null 2>&1 || true
                kubectl rollout status deployment "${_deploy}" -n "${OSMO_NAMESPACE}" --timeout=180s || log_warning "${_deploy} rollout did not complete cleanly"
            fi
        done
    else
        log_warning "Could not refresh SERVICE service_auth. Workflow submission may still fail until 05 is rerun successfully."
    fi
fi

# -----------------------------------------------------------------------------
# Step 12: Configure service_base_url (required for workflow execution)
# -----------------------------------------------------------------------------
# The osmo-ctrl sidecar in every workflow pod needs service_base_url to
# stream logs, report task status, and refresh tokens.
# Store it directly in Postgres so reruns do not rewrite SERVICE.service_auth.

echo ""
log_info "Configuring service_base_url for workflow execution..."

# Detect target URL from Ingress
INGRESS_URL=$(detect_service_url 2>/dev/null || true)

if [[ -n "${OSMO_INGRESS_BASE_URL:-}" ]]; then
    TARGET_SERVICE_URL="${OSMO_INGRESS_BASE_URL}"
    log_info "Using explicit Ingress base URL: ${TARGET_SERVICE_URL}"
elif [[ -n "$INGRESS_URL" ]]; then
    TARGET_SERVICE_URL="${INGRESS_URL}"
    log_info "Auto-detected service URL: ${TARGET_SERVICE_URL}"
else
    log_warning "Could not detect Ingress URL. Skipping service_base_url configuration."
    log_warning "Run ./08-configure-service-url.sh manually after verifying the Ingress."
    TARGET_SERVICE_URL=""
fi

if [[ -n "$TARGET_SERVICE_URL" ]]; then
    _PF_PID=""
    _PF_LOG=""

    _cleanup_pf() {
        stop_port_forward "${_PF_PID:-}" "${_PF_LOG:-}"
    }

    if start_osmo_api_session "${OSMO_NAMESPACE}" 8080 30; then
        _PF_PID=$PORT_FORWARD_PID
        _PF_LOG=$PORT_FORWARD_LOG

        # Login (no-op when bypassing Envoy -- osmo_curl handles auth headers)
        osmo_login "${OSMO_API_PORT}" || true

        # Check current value
        CURRENT_SVC_URL=$(osmo_curl GET "${OSMO_API_URL}/api/configs/service" 2>/dev/null | jq -r '.service_base_url // ""')

        if [[ "$CURRENT_SVC_URL" == "$TARGET_SERVICE_URL" ]]; then
            log_success "service_base_url already configured: ${CURRENT_SVC_URL}"
        else
            if [[ -n "$CURRENT_SVC_URL" && "$CURRENT_SVC_URL" != "null" ]]; then
                log_warning "Updating service_base_url from '${CURRENT_SVC_URL}' to '${TARGET_SERVICE_URL}'"
            fi

            if upsert_osmo_service_base_url_db "${OSMO_NAMESPACE}" "${TARGET_SERVICE_URL}" "${POSTGRES_HOST}" "${POSTGRES_PORT}" "${POSTGRES_DB}" "${POSTGRES_USER}" "${POSTGRES_PASSWORD}"; then
                # Verify
                NEW_SVC_URL=$(osmo_curl GET "${OSMO_API_URL}/api/configs/service" 2>/dev/null | jq -r '.service_base_url // ""')
                if [[ "$NEW_SVC_URL" == "$TARGET_SERVICE_URL" ]]; then
                    log_success "service_base_url configured: ${NEW_SVC_URL}"
                else
                    log_warning "service_base_url verification failed. Run ./08-configure-service-url.sh manually."
                fi
            else
                log_warning "Failed to set service_base_url. Run ./08-configure-service-url.sh manually."
            fi
        fi
        _cleanup_pf
    else
        log_warning "Port-forward not ready. Run ./08-configure-service-url.sh manually."
    fi
fi

echo ""
echo "========================================"
log_success "OSMO Control Plane deployment complete!"
echo "========================================"
echo ""

if [[ "$AUTH_ENABLED" == "true" ]]; then
    # --- Auth-enabled output ---
    echo "Authentication: ENABLED (Keycloak + Envoy sidecars)"
    echo ""
    echo "Keycloak Admin Console:"
    echo "  URL: https://${AUTH_DOMAIN}/admin"
    echo "  Admin: admin / ${KEYCLOAK_ADMIN_PASSWORD}"
    echo ""
    echo "OSMO Access:"
    if [[ -n "$INGRESS_URL" ]]; then
        echo "  OSMO API:   ${INGRESS_URL}/api/version   (unauthenticated -- skipAuthPath)"
        echo "  OSMO Web UI: ${INGRESS_URL}              (redirects to Keycloak login)"
    fi
    echo ""
    echo "Login methods:"
    echo "  Browser: Visit ${INGRESS_URL:-https://<domain>} -- you will be redirected to Keycloak"
    echo "  CLI:     osmo login ${INGRESS_URL:-https://<domain>}"
    echo "           (Opens browser for device authorization flow)"
    echo ""
    echo "Test user: osmo-admin / osmo-admin"
    echo ""
    if [[ "${NEBIUS_SSO_ENABLED:-false}" == "true" && -n "${AUTH_DOMAIN:-}" ]]; then
        _nebius_redirect_uri="https://${AUTH_DOMAIN}/realms/osmo/broker/nebius-sso/endpoint"
        echo "Nebius SSO – IAM redirect URI:"
        echo "  This URI is set per-cluster (script runs 'npc iam oidc-client update' when npc is available)."
        echo "  If you still see 'Invalid redirect_uri', run manually: npc iam oidc-client update <CLIENT_ID> --redirect-uris '${_nebius_redirect_uri}' --authorization-grant-types authorization_code --patch --format yaml"
        echo "  See: applications/osmo/iam-register/README.md"
        echo ""
    fi
    echo "Keycloak realm management (groups, roles, users):"
    echo "  https://nvidia.github.io/OSMO/main/deployment_guide/appendix/authentication/keycloak_setup.html"
    echo ""
    echo "If you see 'OAuth flow failed' or UI spinning: check osmo-ui Envoy logs:"
    echo "  kubectl logs -n ${OSMO_NAMESPACE} deployment/osmo-ui -c envoy --tail=80"
    echo "  Ensure Keycloak client osmo-browser-flow has redirect URI https://<your-host>/oauth2/callback and client secret matches oauth2-proxy-secrets."
    echo ""
else
    # --- No-auth output ---
    if [[ -n "$INGRESS_URL" ]]; then
        echo "OSMO Access (via NGINX Ingress LoadBalancer):"
        echo "  OSMO API: ${INGRESS_URL}/api/version"
        echo "  OSMO UI:  ${INGRESS_URL}"
        echo "  OSMO CLI: osmo login ${INGRESS_URL} --method dev --username admin"
        echo ""
    else
        log_warning "Could not detect Ingress LoadBalancer IP."
        echo "  Check: kubectl get svc -n ${INGRESS_NAMESPACE:-ingress-nginx}"
        echo ""
        echo "  Fallback (port-forward):"
        echo "    kubectl port-forward -n ${OSMO_NAMESPACE} svc/osmo-service 8080:80"
        echo "    URL: http://localhost:8080"
        echo ""
    fi

    echo "NOTE: OSMO API authentication is DISABLED."
    echo "      The API is accessible without tokens."
    echo "      Set DEPLOY_KEYCLOAK=true with TLS to enable Keycloak + Envoy auth."
    echo ""
    echo "Test the API:"
    if [[ -n "$INGRESS_URL" ]]; then
        echo "  curl ${INGRESS_URL}/api/version"
        echo "  curl ${INGRESS_URL}/api/workflow"
    else
        echo "  curl http://localhost:8080/api/version"
        echo "  curl http://localhost:8080/api/workflow"
    fi
    echo ""
    if [[ "${DEPLOY_KEYCLOAK:-false}" == "true" ]]; then
        echo "Keycloak Access (internal only, auth not enforced):"
        echo "  kubectl port-forward -n ${KEYCLOAK_NAMESPACE} svc/keycloak 8081:80"
        echo "  URL: http://localhost:8081"
        echo "  Admin: admin / ${KEYCLOAK_ADMIN_PASSWORD}"
        echo "  Test User: osmo-admin / osmo-admin"
        echo ""
    fi
fi

# --- Diagnostic: dig deeper if you still get 404 on / ---
if [[ -n "${INGRESS_URL:-}" && -n "${AUTH_DOMAIN:-}" && "${DEPLOY_KEYCLOAK:-false}" == "true" ]]; then
    echo ""
    echo "--- Redirect diagnostic (if you see 404, check below) ---"
    echo "Expected: Keycloak https://${AUTH_DOMAIN}  OSMO https://${INGRESS_HOSTNAME:-${OSMO_INGRESS_HOSTNAME:-}}"
    _ui_cm="osmo-ui-envoy-config"
    if kubectl get configmap "$_ui_cm" -n "${OSMO_NAMESPACE}" -o json 2>/dev/null | jq -e '.data["config.yaml"]' &>/dev/null; then
        _redirect_hosts=$(kubectl get configmap "$_ui_cm" -n "${OSMO_NAMESPACE}" -o json | jq -r '.data["config.yaml"]' | grep -oE '(redirect_uri|authEndpoint|auth_provider_cluster)[^"'\'' ]*' | head -5 || true)
        echo "Envoy (osmo-ui) config snippet (redirect/auth): $([ -n "$_redirect_hosts" ] && echo "$_redirect_hosts" | tr '\n' ' ' || echo '(none)')"
        _bad=$(kubectl get configmap "$_ui_cm" -n "${OSMO_NAMESPACE}" -o json | jq -r '.data["config.yaml"]' | grep -oE 'auth-osmo\.local|osmo\.local' | head -1 || true)
        if [[ -n "$_bad" ]]; then
            echo "  WARNING: Envoy config still contains .local ($_bad). Re-run this script or patch the ConfigMap and restart osmo-ui."
        fi
    fi
    echo -n "First redirect from /: "
    _loc=$(curl -sI -k --connect-timeout 5 "${INGRESS_URL}/" 2>/dev/null | grep -i "^location:" | head -1 | sed 's/\r$//') || true
    if [[ -n "$_loc" ]]; then
        echo "$_loc"
        if echo "$_loc" | grep -q '\.local'; then
            echo "  -> Redirect still points to .local; Envoy pods may not have reloaded. Try: kubectl rollout restart deployment osmo-ui -n ${OSMO_NAMESPACE}"
        fi
    else
        echo "(no redirect or unreachable)"
    fi
    echo "--- end diagnostic ---"
    echo ""
fi

echo "Ingress resources:"
kubectl get ingress -n "${OSMO_NAMESPACE}" 2>/dev/null || true
# If using nip.io: hostname must match LoadBalancer IP or the UI "can't be reached"
_lb_ip=$(kubectl get svc -n "${INGRESS_NAMESPACE:-ingress-nginx}" -l app.kubernetes.io/name=ingress-nginx,app.kubernetes.io/component=controller -o jsonpath='{.items[0].status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
if [[ -n "$_lb_ip" && -n "${OSMO_INGRESS_HOSTNAME:-}" ]] && echo "${OSMO_INGRESS_HOSTNAME}" | grep -q '\.nip\.io'; then
    _expected_host="osmo.${_lb_ip//./-}.nip.io"
    if [[ "${OSMO_INGRESS_HOSTNAME}" != "$_expected_host" ]]; then
        echo ""
        echo "--- Hostname vs LoadBalancer (if UI can't be reached) ---"
        echo "  LoadBalancer IP: $_lb_ip  -> use hostname: $_expected_host"
        echo "  Your OSMO_INGRESS_HOSTNAME: ${OSMO_INGRESS_HOSTNAME}"
        echo "  If they differ, set OSMO_INGRESS_HOSTNAME=$_expected_host in osmo-deploy.env, re-run 03b-enable-tls.sh and this script, then open https://$_expected_host"
        if [[ "${NEBIUS_SSO_ENABLED:-false}" == "true" ]]; then
            echo "  Then update Nebius IAM OIDC client redirect URI to https://auth-$_expected_host/realms/osmo/broker/nebius-sso/endpoint (see 'Nebius SSO – IAM redirect URI' above after re-run)."
        fi
        echo "---"
        echo ""
    fi
fi
echo "Next step - Deploy Backend Operator:"
echo "  ./06-deploy-osmo-backend.sh"
echo ""
