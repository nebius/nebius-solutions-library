#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

log() {
    printf '[INFO] %s\n' "$*"
}

warn() {
    printf '[WARN] %s\n' "$*" >&2
}

die() {
    printf '[ERROR] %s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

make_temp_file() {
    local prefix="$1"
    local suffix="${2:-}"
    local tmp_dir="${TMPDIR:-/tmp}"
    local tmp_path=""

    tmp_dir="${tmp_dir%/}"
    tmp_path="$(mktemp "${tmp_dir}/${prefix}.XXXXXX")"
    if [[ -n "${suffix}" ]]; then
        mv "${tmp_path}" "${tmp_path}${suffix}"
        tmp_path="${tmp_path}${suffix}"
        : >"${tmp_path}"
    fi

    printf '%s\n' "${tmp_path}"
}

kubectl_cmd() {
    if [[ -n "${KUBECTL_CONTEXT:-}" ]]; then
        kubectl --context "${KUBECTL_CONTEXT}" "$@"
    else
        kubectl "$@"
    fi
}

port_in_use() {
    local port="$1"
    lsof -nP -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1
}

select_port_forward_port() {
    local preferred_port="${PORT_FORWARD_PORT:-18080}"
    local candidate=""

    if ! port_in_use "${preferred_port}"; then
        PORT_FORWARD_PORT="${preferred_port}"
        return 0
    fi

    warn "Local port ${preferred_port} is already in use; selecting another port for the temporary Keycloak port-forward"
    for candidate in $(seq 18081 18120); do
        if ! port_in_use "${candidate}"; then
            PORT_FORWARD_PORT="${candidate}"
            return 0
        fi
    done

    die "Could not find a free local port for the temporary Keycloak port-forward"
}

start_port_forward() {
    PF_LOG="$(make_temp_file "keycloak-pf" ".log")"
    kubectl_cmd port-forward -n "${NAMESPACE}" "svc/${RELEASE_NAME}" "${PORT_FORWARD_PORT}:80" >"${PF_LOG}" 2>&1 &
    PORT_FORWARD_PID=$!

    for _ in $(seq 1 30); do
        if curl -fsS "http://127.0.0.1:${PORT_FORWARD_PORT}/realms/master" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done

    warn "Temporary port-forward log:"
    cat "${PF_LOG}" >&2 || true
    die "Keycloak port-forward did not become ready on localhost:${PORT_FORWARD_PORT}"
}

stop_port_forward() {
    if [[ -n "${PORT_FORWARD_PID:-}" ]]; then
        kill "${PORT_FORWARD_PID}" >/dev/null 2>&1 || true
        wait "${PORT_FORWARD_PID}" 2>/dev/null || true
    fi

    if [[ -n "${PF_LOG:-}" && -f "${PF_LOG}" ]]; then
        rm -f "${PF_LOG}"
    fi
}

kc_login() {
    curl -fsS -X POST "http://127.0.0.1:${PORT_FORWARD_PORT}/realms/master/protocol/openid-connect/token" \
      --data-urlencode "client_id=admin-cli" \
      --data-urlencode "username=admin" \
      --data-urlencode "password=${KEYCLOAK_ADMIN_PASSWORD}" \
      --data-urlencode "grant_type=password" | jq -r '.access_token // empty'
}

kc_http() {
    local method="$1"
    local path="$2"
    local data_file="${3:-}"
    local out_file="${4:-}"
    local code=""
    local curl_args=()

    curl_args=(-sS -o "${out_file}" -w "%{http_code}" -X "${method}" "http://127.0.0.1:${PORT_FORWARD_PORT}${path}" -H "Authorization: Bearer ${TOKEN}")
    if [[ -n "${data_file}" ]]; then
        curl_args+=(-H "Content-Type: application/json" -d @"${data_file}")
    fi
    code=$(curl "${curl_args[@]}")
    printf '%s' "${code}"
}

prepare_realm_file() {
    REALM_IMPORT_FILE="$(make_temp_file "osmo-realm" ".json")"
    cp "${REALM_TEMPLATE}" "${REALM_IMPORT_FILE}"
    sed -i.bak "s|https://default.com|https://${OSMO_INGRESS_HOSTNAME}|g" "${REALM_IMPORT_FILE}"
    sed -i.bak 's/"secret": "[*][*]*"/"secret": "'"${OIDC_CLIENT_SECRET}"'"/' "${REALM_IMPORT_FILE}"
    rm -f "${REALM_IMPORT_FILE}.bak"
}

ensure_osmo_realm() {
    local code=""
    local resp=""

    resp="$(make_temp_file "keycloak-realm-check" ".json")"
    code=$(kc_http "GET" "/admin/realms/osmo" "" "${resp}")
    if [[ "${code}" == "200" ]]; then
        log "Keycloak realm 'osmo' already exists; keeping it and applying updates"
        rm -f "${resp}"
        return 0
    fi
    rm -f "${resp}"

    log "Importing OSMO realm template"
    resp="$(make_temp_file "keycloak-realm-import" ".json")"
    code=$(kc_http "POST" "/admin/realms" "${REALM_IMPORT_FILE}" "${resp}")
    if [[ "${code}" != "201" && "${code}" != "204" ]]; then
        cat "${resp}" >&2 || true
        rm -f "${resp}"
        die "Realm import failed with HTTP ${code}"
    fi
    rm -f "${resp}"
}

client_uuid() {
    local client_id="$1"
    local resp=""
    local code=""

    resp="$(make_temp_file "keycloak-client" ".json")"
    code=$(kc_http "GET" "/admin/realms/osmo/clients?clientId=${client_id}" "" "${resp}")
    if [[ "${code}" != "200" ]]; then
        rm -f "${resp}"
        return 1
    fi
    jq -r '.[0].id // empty' "${resp}"
    rm -f "${resp}"
}

ensure_browser_redirects() {
    local client_id="$1"
    local resp=""
    local update=""
    local code=""

    resp="$(make_temp_file "browser-client" ".json")"
    update="$(make_temp_file "browser-client-update" ".json")"

    code=$(kc_http "GET" "/admin/realms/osmo/clients/${client_id}" "" "${resp}")
    [[ "${code}" == "200" ]] || die "Could not fetch osmo-browser-flow client (HTTP ${code})"

    jq \
      --arg base "https://${OSMO_INGRESS_HOSTNAME}" \
      '.redirectUris = [
        ($base + "/oauth2/callback"),
        ($base + "/getAToken"),
        ($base + "/api/auth/getAToken"),
        ($base + "/setup/getAToken")
      ]' "${resp}" >"${update}"

    code=$(kc_http "PUT" "/admin/realms/osmo/clients/${client_id}" "${update}" "${resp}")
    rm -f "${resp}" "${update}"
    [[ "${code}" == "200" || "${code}" == "204" ]] || die "Failed to update osmo-browser-flow redirect URIs (HTTP ${code})"
}

ensure_browser_secret() {
    local client_id="$1"
    local resp=""
    local update=""
    local verify=""
    local code=""
    local actual_secret=""

    resp="$(make_temp_file "browser-secret" ".json")"
    update="$(make_temp_file "browser-secret-update" ".json")"
    verify="$(make_temp_file "browser-secret-verify" ".json")"

    code=$(kc_http "GET" "/admin/realms/osmo/clients/${client_id}" "" "${resp}")
    [[ "${code}" == "200" ]] || die "Could not fetch osmo-browser-flow client for secret update (HTTP ${code})"

    jq --arg secret "${OIDC_CLIENT_SECRET}" '.secret = $secret' "${resp}" >"${update}"
    code=$(kc_http "PUT" "/admin/realms/osmo/clients/${client_id}" "${update}" "${resp}")
    [[ "${code}" == "200" || "${code}" == "204" ]] || die "Failed to update osmo-browser-flow client secret (HTTP ${code})"

    code=$(kc_http "GET" "/admin/realms/osmo/clients/${client_id}/client-secret" "" "${verify}")
    [[ "${code}" == "200" ]] || die "Failed to verify osmo-browser-flow client secret (HTTP ${code})"
    actual_secret="$(jq -r '.value // empty' "${verify}")"

    rm -f "${resp}" "${update}" "${verify}"
    [[ "${actual_secret}" == "${OIDC_CLIENT_SECRET}" ]] || die "Keycloak returned a different osmo-browser-flow client secret than expected"
}

ensure_device_audience_mapper() {
    local client_id="$1"
    local resp=""
    local payload=""
    local code=""
    local exists=""

    resp="$(make_temp_file "device-mappers" ".json")"
    payload="$(make_temp_file "device-mapper-create" ".json")"

    code=$(kc_http "GET" "/admin/realms/osmo/clients/${client_id}/protocol-mappers/models" "" "${resp}")
    [[ "${code}" == "200" ]] || die "Failed to inspect osmo-device mappers (HTTP ${code})"

    exists="$(jq -r '.[] | select(.name == "audience osmo-device") | .id // empty' "${resp}" | head -1 || true)"
    if [[ -n "${exists}" ]]; then
        rm -f "${resp}" "${payload}"
        return 0
    fi

    cat >"${payload}" <<'EOF'
{"name":"audience osmo-device","protocol":"openid-connect","protocolMapper":"oidc-audience-mapper","consentRequired":false,"config":{"included.custom.audience":"osmo-device","access.token.claim":"true","id.token.claim":"false"}}
EOF

    code=$(kc_http "POST" "/admin/realms/osmo/clients/${client_id}/protocol-mappers/models" "${payload}" "${resp}")
    rm -f "${resp}" "${payload}"
    [[ "${code}" == "201" || "${code}" == "204" ]] || die "Failed to add osmo-device audience mapper (HTTP ${code})"
}

ensure_frontend_url() {
    local resp=""
    local update=""
    local code=""

    resp="$(make_temp_file "realm-osmo" ".json")"
    update="$(make_temp_file "realm-osmo-update" ".json")"

    code=$(kc_http "GET" "/admin/realms/osmo" "" "${resp}")
    [[ "${code}" == "200" ]] || die "Failed to fetch realm 'osmo' (HTTP ${code})"

    jq --arg url "https://${KEYCLOAK_HOSTNAME}" '.attributes = ((.attributes // {}) + {"frontendUrl": $url})' "${resp}" >"${update}"
    code=$(kc_http "PUT" "/admin/realms/osmo" "${update}" "${resp}")
    rm -f "${resp}" "${update}"
    [[ "${code}" == "200" || "${code}" == "204" ]] || die "Failed to update realm frontendUrl (HTTP ${code})"
}

ensure_nebius_first_login_flow() {
    local resp=""
    local execs=""
    local body=""
    local code=""
    local review_id=""
    local handle_id=""

    resp="$(make_temp_file "kc-flow-copy" ".json")"
    body="$(make_temp_file "kc-flow-body" ".json")"
    execs="$(make_temp_file "kc-flow-execs" ".json")"

    kc_http "DELETE" "/admin/realms/osmo/authentication/flows/nebius-first-login" "" "${resp}" >/dev/null || true

    printf '%s' '{"newName":"nebius-first-login"}' >"${body}"
    code=$(kc_http "POST" "/admin/realms/osmo/authentication/flows/first%20broker%20login/copy" "${body}" "${resp}")
    if [[ "${code}" != "201" && "${code}" != "204" && "${code}" != "409" && "${code}" != "400" ]]; then
        warn "Could not copy first broker login flow (HTTP ${code}); Keycloak will use the default flow"
        rm -f "${resp}" "${body}" "${execs}"
        printf '%s' "first broker login"
        return 0
    fi

    code=$(kc_http "GET" "/admin/realms/osmo/authentication/flows/nebius-first-login/executions" "" "${execs}")
    if [[ "${code}" != "200" ]]; then
        warn "Could not inspect nebius-first-login executions (HTTP ${code}); using the default flow"
        rm -f "${resp}" "${body}" "${execs}"
        printf '%s' "first broker login"
        return 0
    fi

    review_id="$(jq -r '.[] | select(.displayName == "Review Profile") | .id // empty' "${execs}" | head -1 || true)"
    handle_id="$(jq -r '.[] | select(.displayName == "Handle Existing Account") | .id // empty' "${execs}" | head -1 || true)"

    if [[ -n "${review_id}" ]]; then
        printf '{"id":"%s","requirement":"REQUIRED"}' "${review_id}" >"${body}"
        kc_http "PUT" "/admin/realms/osmo/authentication/flows/nebius-first-login/executions" "${body}" "${resp}" >/dev/null || true
    fi
    if [[ -n "${handle_id}" ]]; then
        printf '{"id":"%s","requirement":"DISABLED"}' "${handle_id}" >"${body}"
        kc_http "PUT" "/admin/realms/osmo/authentication/flows/nebius-first-login/executions" "${body}" "${resp}" >/dev/null || true
    fi

    rm -f "${resp}" "${body}" "${execs}"
    printf '%s' "nebius-first-login"
}

ensure_nebius_sso_idp() {
    local resp=""
    local payload=""
    local mapper=""
    local code=""
    local flow_alias=""

    if [[ "${NEBIUS_SSO_ENABLED}" != "true" ]]; then
        log "NEBIUS_SSO_ENABLED is not true; skipping Nebius SSO identity provider configuration"
        return 0
    fi

    if [[ -z "${NEBIUS_SSO_CLIENT_ID:-}" || -z "${NEBIUS_SSO_CLIENT_SECRET:-}" || -z "${NEBIUS_SSO_ISSUER_URL:-}" ]]; then
        warn "Nebius SSO is enabled but one or more values are missing; skipping IdP creation"
        return 0
    fi

    flow_alias="$(ensure_nebius_first_login_flow)"
    resp="$(make_temp_file "kc-idp" ".json")"
    payload="$(make_temp_file "kc-idp-body" ".json")"
    mapper="$(make_temp_file "kc-idp-mapper" ".json")"

    kc_http "DELETE" "/admin/realms/osmo/identity-provider/instances/nebius-sso" "" "${resp}" >/dev/null || true

    jq -n \
      --arg issuer "${NEBIUS_SSO_ISSUER_URL}" \
      --arg client_id "${NEBIUS_SSO_CLIENT_ID}" \
      --arg client_secret "${NEBIUS_SSO_CLIENT_SECRET}" \
      --arg flow_alias "${flow_alias}" \
      '{
        alias: "nebius-sso",
        providerId: "oidc",
        displayName: "Nebius SSO",
        enabled: true,
        authenticateByDefault: false,
        trustEmail: true,
        storeToken: false,
        addReadTokenRoleOnCreate: false,
        linkOnly: false,
        firstBrokerLoginFlowAlias: $flow_alias,
        config: {
          issuer: $issuer,
          authorizationUrl: ($issuer + "/oauth2/authorize"),
          tokenUrl: ($issuer + "/oauth2/token"),
          clientId: $client_id,
          clientSecret: $client_secret,
          clientAuthMethod: "client_secret_basic",
          defaultScope: "openid",
          syncMode: "FORCE",
          useJwksUrl: "true",
          pkceEnabled: "true",
          pkceMethod: "S256"
        }
      }' >"${payload}"

    code=$(kc_http "POST" "/admin/realms/osmo/identity-provider/instances" "${payload}" "${resp}")
    [[ "${code}" == "201" || "${code}" == "204" ]] || {
        cat "${resp}" >&2 || true
        rm -f "${resp}" "${payload}" "${mapper}"
        die "Failed to create Nebius SSO identity provider (HTTP ${code})"
    }

    jq -n \
      --arg claim "${NEBIUS_SSO_GROUP_ATTRIBUTE}" \
      '{
        name: "groups",
        identityProviderMapper: "oidc-advanced-group-idp-mapper",
        identityProviderAlias: "nebius-sso",
        config: {
          syncMode: "INHERIT",
          "groups.claim": $claim
        }
      }' >"${mapper}"

    code=$(kc_http "POST" "/admin/realms/osmo/identity-provider/instances/nebius-sso/mappers" "${mapper}" "${resp}")
    if [[ "${code}" != "201" && "${code}" != "204" && "${code}" != "409" ]]; then
        warn "Nebius SSO IdP mapper creation returned HTTP ${code}"
    fi

    rm -f "${resp}" "${payload}" "${mapper}"
}

ensure_default_user_group() {
    local groups=""
    local defaults=""
    local resp=""
    local code=""
    local user_group_id=""
    local already_default=""

    groups="$(make_temp_file "kc-groups" ".json")"
    defaults="$(make_temp_file "kc-default-groups" ".json")"
    resp="$(make_temp_file "kc-default-group-resp" ".json")"

    code=$(kc_http "GET" "/admin/realms/osmo/groups?search=User" "" "${groups}")
    [[ "${code}" == "200" ]] || die "Failed to query Keycloak groups (HTTP ${code})"
    user_group_id="$(jq -r '.[] | select(.name == "User") | .id // empty' "${groups}" | head -1 || true)"
    [[ -n "${user_group_id}" ]] || {
        rm -f "${groups}" "${defaults}" "${resp}"
        die "Could not find the 'User' group in the osmo realm"
    }

    code=$(kc_http "GET" "/admin/realms/osmo/default-groups" "" "${defaults}")
    [[ "${code}" == "200" ]] || die "Failed to query Keycloak default groups (HTTP ${code})"
    already_default="$(jq -r --arg gid "${user_group_id}" '.[] | select(.id == $gid) | .id // empty' "${defaults}" | head -1 || true)"
    if [[ -n "${already_default}" ]]; then
        rm -f "${groups}" "${defaults}" "${resp}"
        return 0
    fi

    code=$(kc_http "PUT" "/admin/realms/osmo/default-groups/${user_group_id}" "" "${resp}")
    rm -f "${groups}" "${defaults}" "${resp}"
    [[ "${code}" == "204" ]] || die "Failed to set the default User group (HTTP ${code})"
}

ensure_breakglass_user() {
    local query=""
    local resp=""
    local payload=""
    local group_resp=""
    local code=""
    local user_id=""
    local admin_group_id=""

    [[ "${CREATE_BREAKGLASS_USER}" == "true" ]] || return 0

    query="$(make_temp_file "kc-user-query" ".json")"
    resp="$(make_temp_file "kc-user-resp" ".json")"
    payload="$(make_temp_file "kc-user-payload" ".json")"
    group_resp="$(make_temp_file "kc-group-query" ".json")"

    code=$(kc_http "GET" "/admin/realms/osmo/users?username=osmo-admin" "" "${query}")
    [[ "${code}" == "200" ]] || die "Failed to query osmo-admin user (HTTP ${code})"
    user_id="$(jq -r '.[0].id // empty' "${query}")"

    if [[ -z "${user_id}" ]]; then
        cat >"${payload}" <<'EOF'
{"username":"osmo-admin","enabled":true,"emailVerified":true,"firstName":"OSMO","lastName":"Admin","email":"osmo-admin@example.com","credentials":[{"type":"password","value":"osmo-admin","temporary":false}]}
EOF
        code=$(kc_http "POST" "/admin/realms/osmo/users" "${payload}" "${resp}")
        [[ "${code}" == "201" || "${code}" == "204" || "${code}" == "409" ]] || die "Failed to create local osmo-admin user (HTTP ${code})"

        code=$(kc_http "GET" "/admin/realms/osmo/users?username=osmo-admin" "" "${query}")
        [[ "${code}" == "200" ]] || die "Failed to re-query osmo-admin user (HTTP ${code})"
        user_id="$(jq -r '.[0].id // empty' "${query}")"
    fi

    code=$(kc_http "GET" "/admin/realms/osmo/groups?search=Admin" "" "${group_resp}")
    [[ "${code}" == "200" ]] || die "Failed to query Admin group (HTTP ${code})"
    admin_group_id="$(jq -r '.[] | select(.name == "Admin") | .id // empty' "${group_resp}" | head -1 || true)"
    [[ -n "${admin_group_id}" ]] || die "Could not find the 'Admin' group in Keycloak"

    code=$(kc_http "PUT" "/admin/realms/osmo/users/${user_id}/groups/${admin_group_id}" "" "${resp}")
    rm -f "${query}" "${resp}" "${payload}" "${group_resp}"
    [[ "${code}" == "204" || "${code}" == "201" || "${code}" == "409" ]] || die "Failed to assign osmo-admin to the Admin group (HTTP ${code})"
}

cleanup() {
    stop_port_forward

    [[ -n "${REALM_IMPORT_FILE:-}" && -f "${REALM_IMPORT_FILE}" ]] && rm -f "${REALM_IMPORT_FILE}"
}

require_command kubectl
require_command curl
require_command jq

: "${NAMESPACE:?NAMESPACE is required}"
: "${RELEASE_NAME:?RELEASE_NAME is required}"
: "${KEYCLOAK_HOSTNAME:?KEYCLOAK_HOSTNAME is required}"
: "${OSMO_INGRESS_HOSTNAME:?OSMO_INGRESS_HOSTNAME is required}"
: "${KEYCLOAK_ADMIN_PASSWORD:?KEYCLOAK_ADMIN_PASSWORD is required}"
: "${OIDC_CLIENT_SECRET:?OIDC_CLIENT_SECRET is required}"

REALM_TEMPLATE="${REALM_TEMPLATE:-${SCRIPT_DIR}/../config/keycloak/realm.json}"
PORT_FORWARD_PORT="${PORT_FORWARD_PORT:-18080}"
CREATE_BREAKGLASS_USER="${CREATE_BREAKGLASS_USER:-true}"
NEBIUS_SSO_ENABLED="${NEBIUS_SSO_ENABLED:-false}"
NEBIUS_SSO_ISSUER_URL="${NEBIUS_SSO_ISSUER_URL:-https://auth.nebius.com}"
NEBIUS_SSO_GROUP_ATTRIBUTE="${NEBIUS_SSO_GROUP_ATTRIBUTE:-groups}"

[[ -f "${REALM_TEMPLATE}" ]] || die "OSMO realm template not found at ${REALM_TEMPLATE}"
kubectl_cmd get namespace "${NAMESPACE}" >/dev/null 2>&1 || die "Namespace ${NAMESPACE} not found"
kubectl_cmd wait --for=condition=Ready pod -l app.kubernetes.io/name=keycloak -n "${NAMESPACE}" --timeout=600s >/dev/null

trap cleanup EXIT

prepare_realm_file
select_port_forward_port
start_port_forward

TOKEN="$(kc_login)"
[[ -n "${TOKEN}" ]] || die "Failed to get a Keycloak admin token"

ensure_osmo_realm

BROWSER_CLIENT_ID="$(client_uuid "osmo-browser-flow" || true)"
[[ -n "${BROWSER_CLIENT_ID}" ]] || die "Could not find the osmo-browser-flow client in Keycloak"
ensure_browser_redirects "${BROWSER_CLIENT_ID}"
ensure_browser_secret "${BROWSER_CLIENT_ID}"

DEVICE_CLIENT_ID="$(client_uuid "osmo-device" || true)"
[[ -n "${DEVICE_CLIENT_ID}" ]] || die "Could not find the osmo-device client in Keycloak"
ensure_device_audience_mapper "${DEVICE_CLIENT_ID}"

ensure_frontend_url
ensure_nebius_sso_idp
ensure_default_user_group
ensure_breakglass_user

log "Keycloak bootstrap complete"
