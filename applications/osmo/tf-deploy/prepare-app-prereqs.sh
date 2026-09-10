#!/bin/bash
#
# Unified tf-deploy pre-apply workflow.
#
# This script is the source of truth for:
#   - sync-kubeconfig
#   - register-keycloak-oidc
#   - prepare-app-prereqs
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

usage_main() {
    cat <<'EOF'
Usage:
  ./prepare-app-prereqs.sh [command] [options]

Commands:
  prepare              Materialize kubeconfig, bootstrap ingress-nginx when needed, discover the public ingress IP, and register/update Nebius SSO. Default command.
  cleanup-dns          Delete the managed public DNS A recordsets for the current OSMO and Keycloak hostnames.
  sync-kubeconfig      Materialize kubeconfig from infra outputs.
  register-oidc        Register/update the Nebius IAM OIDC client for Keycloak and force-replace the client secret.
  -h, --help           Show this help.

Examples:
  ./prepare-app-prereqs.sh
  ./prepare-app-prereqs.sh prepare
  ./prepare-app-prereqs.sh sync-kubeconfig --help
EOF
}

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

write_file_atomically() {
    local dest="$1"
    local tmp=""

    mkdir -p "$(dirname "${dest}")"
    tmp="$(mktemp "${TMPDIR:-/tmp}/tf-deploy-write.XXXXXX")"
    cat >"${tmp}"
    chmod 600 "${tmp}"
    mv "${tmp}" "${dest}"
}

write_export_line() {
    local name="$1"
    local value="$2"

    printf 'export %s=%q\n' "${name}" "${value}"
}

load_local_env_init() {
    local env_script="${SCRIPT_DIR}/nebius-env-init.sh"

    [[ -f "${env_script}" ]] || return 0

    # shellcheck disable=SC1090
    TF_DEPLOY_ENV_INIT_EXPORT_ONLY=1 TF_DEPLOY_ENV_INIT_QUIET=1 source "${env_script}"
}

terraform_output_json_value() {
    local terraform_dir="$1"
    local output_name="$2"
    local tmp_json=""
    local value=""

    tmp_json="$(make_temp_file "tf-deploy-output" ".json")"

    if ! terraform -chdir="${terraform_dir}" output -json >"${tmp_json}" 2>/dev/null; then
        rm -f "${tmp_json}"
        return 1
    fi

    value="$(jq -r --arg name "${output_name}" '.[$name].value // empty' "${tmp_json}" 2>/dev/null || true)"
    rm -f "${tmp_json}"

    [[ -n "${value}" ]] || return 1
    printf '%s' "${value}"
}

terraform_output_object_field() {
    local terraform_dir="$1"
    local output_name="$2"
    local field_name="$3"
    local tmp_json=""
    local value=""

    tmp_json="$(make_temp_file "tf-deploy-output" ".json")"

    if ! terraform -chdir="${terraform_dir}" output -json >"${tmp_json}" 2>/dev/null; then
        rm -f "${tmp_json}"
        return 1
    fi

    value="$(jq -r --arg name "${output_name}" --arg field "${field_name}" '.[$name].value[$field] // empty' "${tmp_json}" 2>/dev/null || true)"
    rm -f "${tmp_json}"

    [[ -n "${value}" ]] || return 1
    printf '%s' "${value}"
}

mysterybox_payload_get_by_key() {
    local secret_id="$1"
    local key="$2"
    local result=""
    local value=""

    result="$(nebius mysterybox v1 payload get-by-key --secret-id "${secret_id}" --key "${key}" --format json 2>/dev/null || true)"
    if [[ -z "${result}" ]]; then
        result="$(nebius mysterybox payload get-by-key --secret-id "${secret_id}" --key "${key}" --format json 2>/dev/null || true)"
    fi

    [[ -n "${result}" ]] || return 1

    value="$(jq -r '.data.string_value // empty' <<<"${result}" 2>/dev/null || true)"
    [[ -n "${value}" ]] || return 1
    printf '%s' "${value}"
}

write_prepare_tf_secret_overrides() {
    local env_path="$1"
    local infra_dir="${SCRIPT_DIR}/infra"
    local storage_secret_reference_id=""
    local postgres_secret_reference_id=""
    local storage_access_key_id=""
    local storage_secret_access_key=""
    local postgres_password=""
    local existing_content=""
    local filtered_content=""

    [[ -f "${env_path}" ]] || die "Expected env file to exist before adding Terraform secret overrides: ${env_path}"

    require_command terraform
    require_command nebius
    require_command jq

    storage_secret_reference_id="$(terraform_output_json_value "${infra_dir}" "storage_secret_reference_id" || true)"
    postgres_secret_reference_id="$(terraform_output_object_field "${infra_dir}" "mysterybox_secrets" "postgresql_secret_id" || true)"
    storage_access_key_id="$(terraform_output_object_field "${infra_dir}" "storage_credentials" "access_key_id" || true)"
    postgres_password="$(terraform_output_json_value "${infra_dir}" "postgresql_password" || true)"

    if [[ -n "${storage_secret_reference_id}" ]]; then
        storage_secret_access_key="$(mysterybox_payload_get_by_key "${storage_secret_reference_id}" "secret" || true)"
        [[ -n "${storage_secret_access_key}" ]] || die "Failed to read storage secret payload via nebius CLI for secret ${storage_secret_reference_id}. Grant MysteryBox payload-read access or provide TF_VAR_storage_secret_access_key explicitly."
    fi

    if [[ -z "${postgres_password}" && -n "${postgres_secret_reference_id}" ]]; then
        postgres_password="$(mysterybox_payload_get_by_key "${postgres_secret_reference_id}" "password" || true)"
        [[ -n "${postgres_password}" ]] || die "Failed to read PostgreSQL password via nebius CLI for secret ${postgres_secret_reference_id}. Grant MysteryBox payload-read access or provide TF_VAR_postgres_password explicitly."
    fi

    existing_content="$(cat "${env_path}")"
    filtered_content="$(
        printf '%s\n' "${existing_content}" \
        | sed \
            -e '/^export TF_VAR_storage_access_key_id=/d' \
            -e '/^export TF_VAR_storage_secret_access_key=/d' \
            -e '/^export TF_VAR_postgres_password=/d'
    )"

    {
        printf '%s\n' "${filtered_content}" | sed '/^$/N;/^\n$/D'
        if [[ -n "${storage_access_key_id}" ]]; then
            write_export_line "TF_VAR_storage_access_key_id" "${storage_access_key_id}"
        fi
        if [[ -n "${storage_secret_access_key}" ]]; then
            write_export_line "TF_VAR_storage_secret_access_key" "${storage_secret_access_key}"
        fi
        if [[ -n "${postgres_password}" ]]; then
            write_export_line "TF_VAR_postgres_password" "${postgres_password}"
        fi
    } | write_file_atomically "${env_path}"

    log "Added Terraform secret overrides to ${env_path}"
}

write_prepare_nipio_no_npc_env() {
    local env_path="$1"

    [[ "${OSMO_BASE_DOMAIN:-}" == "nip.io" ]] || die "The no-npc fallback only supports OSMO_BASE_DOMAIN=nip.io."
    [[ -n "${OSMO_INGRESS_HOSTNAME:-}" ]] || die "OSMO_INGRESS_HOSTNAME is empty after nip.io ingress bootstrap."
    [[ -n "${KEYCLOAK_HOSTNAME:-}" ]] || die "KEYCLOAK_HOSTNAME is empty after nip.io ingress bootstrap."

    {
        printf '# Generated by %s prepare\n' "${SCRIPT_DIR}/prepare-app-prereqs.sh"
        if [[ -n "${PREPARE_INGRESS_PUBLIC_IP:-}" ]]; then
            printf '# Ingress public IP: %s\n' "${PREPARE_INGRESS_PUBLIC_IP}"
        fi
        printf '# Nebius SSO disabled because npc is not available for the nip.io fallback.\n'
        printf '# The final app apply still enables auth and TLS; log in with the local Keycloak breakglass user after deployment.\n'
        write_export_line "OSMO_INGRESS_HOSTNAME" "${OSMO_INGRESS_HOSTNAME}"
        write_export_line "TF_VAR_ingress_hostname" "${OSMO_INGRESS_HOSTNAME}"
        write_export_line "KEYCLOAK_HOSTNAME" "${KEYCLOAK_HOSTNAME}"
        write_export_line "TF_VAR_keycloak_hostname" "${KEYCLOAK_HOSTNAME}"
        write_export_line "NEBIUS_SSO_ENABLED" "false"
        write_export_line "TF_VAR_nebius_sso_enabled" "false"
    } | write_file_atomically "${env_path}"

    log "Wrote env snippet to ${env_path}"
}

json_find_client_id() {
    local raw="$1"

    jq -r '
      [
        .. | objects | (.metadata?.id?, .id?, .client_id?)
        | select(type == "string" and length > 0)
      ] | .[0] // empty
    ' <<<"${raw}" 2>/dev/null || true
}

json_find_client_secret() {
    local raw="$1"

    jq -r '
      [
        .. | objects | (.client_secret?, .secret?, .value?)
        | select(type == "string" and length > 0)
      ] | .[0] // empty
    ' <<<"${raw}" 2>/dev/null || true
}

json_find_client_id_by_name() {
    local raw="$1"
    local name="$2"

    jq -r --arg name "${name}" '
      [
        .. | objects
        | select((.metadata?.name? // .name? // "") == $name)
        | (.metadata?.id?, .id?, .client_id?)
        | select(type == "string" and length > 0)
      ] | .[0] // empty
    ' <<<"${raw}" 2>/dev/null || true
}

json_client_has_redirect_uri() {
    local raw="$1"
    local redirect_uri="$2"

    jq -e --arg redirect_uri "${redirect_uri}" '
      [
        .. | objects | (.spec?.redirect_uris? // .redirect_uris? // empty)
      ]
      | flatten
      | index($redirect_uri) != null
    ' <<<"${raw}" >/dev/null 2>&1
}

npc_extract_json_payload() {
    local raw="$1"
    local candidate=""
    local json_payload=""
    local line=""

    candidate="$(
        awk '
          found { print; next }
          /^[[:space:]]*[{[]/ { found=1; print }
        ' <<<"${raw}"
    )"

    [[ -n "${candidate}" ]] || return 1

    while IFS= read -r line || [[ -n "${line}" ]]; do
        if [[ -n "${json_payload}" ]]; then
            json_payload+=$'\n'
        fi
        json_payload+="${line}"

        if jq -e . >/dev/null 2>&1 <<<"${json_payload}"; then
            printf '%s\n' "${json_payload}"
            return 0
        fi
    done <<<"${candidate}"

    return 1
}

npc_run_json() {
    local description="$1"
    shift

    local output=""
    local json_payload=""

    if ! output=$(npc "$@" --format json 2>&1); then
        die "${description}. npc output: ${output}"
    fi

    json_payload="$(npc_extract_json_payload "${output}")"
    [[ -n "${json_payload}" ]] || die "${description}. npc output did not contain JSON: ${output}"
    jq -e . >/dev/null 2>&1 <<<"${json_payload}" || die "${description}. npc output contained invalid JSON: ${output}"

    printf '%s' "${json_payload}"
}

usage_sync_kubeconfig() {
    cat <<'EOF'
Usage:
  ./prepare-app-prereqs.sh sync-kubeconfig [options]

Options:
  --infra-dir PATH         Terraform infra root. Defaults to ./infra.
  --kubeconfig PATH        Kubeconfig path to write. Defaults to ./generated/kubeconfig.
  --context-name NAME      Kubeconfig context name. Defaults to the Terraform cluster name.
  --write-env PATH         Env file to write. Defaults to ./cluster-access.env.
  --external               Force the external MK8s endpoint.
  --internal               Force the internal MK8s endpoint.
  -h, --help               Show this help.
EOF
}

sync_infer_endpoint_mode() {
    local endpoint="$1"

    if [[ -n "${SYNC_ENDPOINT_MODE}" ]]; then
        return 0
    fi

    if [[ "${endpoint}" == *"://pu."* ]]; then
        SYNC_ENDPOINT_MODE="external"
    else
        SYNC_ENDPOINT_MODE="internal"
    fi
}

sync_write_env_file() {
    {
        printf '# Generated by %s sync-kubeconfig\n' "${SCRIPT_DIR}/prepare-app-prereqs.sh"
        write_export_line "KUBECONFIG" "${SYNC_KUBECONFIG_PATH}"
        write_export_line "KUBECONFIG_CONTEXT" "${SYNC_CONTEXT_NAME}"
        write_export_line "TF_VAR_kubeconfig_path" "${SYNC_KUBECONFIG_PATH}"
        write_export_line "TF_VAR_kubeconfig_context" "${SYNC_CONTEXT_NAME}"
        write_export_line "TF_DEPLOY_CLUSTER_ID" "${SYNC_CLUSTER_ID}"
        write_export_line "TF_DEPLOY_CLUSTER_NAME" "${SYNC_CLUSTER_NAME}"
    } | write_file_atomically "${SYNC_WRITE_ENV_PATH}"

    log "Wrote kubeconfig exports to ${SYNC_WRITE_ENV_PATH}"
}

prepare_print_dns_record_instructions() {
    local lb_ip="$1"
    local osmo_hostname="$2"
    local keycloak_hostname="$3"

    cat <<EOF

Public DNS records required before the final app apply:
  A ${osmo_hostname} -> ${lb_ip}
  A ${keycloak_hostname} -> ${lb_ip}

If you are using tls_mode=cert-manager, wait for both names to resolve publicly
before running the final terraform apply so ACME HTTP-01 can succeed.
EOF
}

prepare_upsert_public_dns_records() {
    local lb_ip="$1"

    [[ "${OSMO_BASE_DOMAIN:-}" != "nip.io" ]] || return 0

    if [[ -z "${DNS_ZONE_ID:-}" || -z "${DNS_NPC_PROFILE:-}" ]]; then
        log "DNS_ZONE_ID or DNS_NPC_PROFILE is not set; skipping automatic DNS record management"
        prepare_print_dns_record_instructions "${lb_ip}" "${OSMO_INGRESS_HOSTNAME}" "${KEYCLOAK_HOSTNAME}"
        return 0
    fi

    PUBLIC_DNS_TARGET_IP="${lb_ip}" \
    ACTION="upsert" \
    OSMO_BASE_DOMAIN="${OSMO_BASE_DOMAIN}" \
    OSMO_HOSTNAME="${OSMO_INGRESS_HOSTNAME}" \
    KEYCLOAK_HOSTNAME="${KEYCLOAK_HOSTNAME}" \
    DNS_NPC_PROFILE="${DNS_NPC_PROFILE}" \
    DNS_ZONE_ID="${DNS_ZONE_ID}" \
    /bin/bash "${SCRIPT_DIR}/scripts/manage-public-dns-records.sh"
}

prepare_bootstrap_ingress() {
    local ingress_namespace="${INGRESS_NAMESPACE:-ingress-nginx}"
    local ingress_release_name="${INGRESS_RELEASE_NAME:-ingress-nginx}"
    local ingress_service_name="${INGRESS_CONTROLLER_SERVICE_NAME:-${ingress_release_name}-controller}"
    local lb_ip=""
    local attempt=0
    local -a kubectl_cmd=(kubectl)
    local bootstrap_ingress_hostname="${OSMO_INGRESS_HOSTNAME:-osmo.invalid}"
    local bootstrap_keycloak_hostname="${KEYCLOAK_HOSTNAME:-auth-osmo.invalid}"

    if [[ -n "${SYNC_CONTEXT_NAME:-}" ]]; then
        kubectl_cmd+=(--context "${SYNC_CONTEXT_NAME}")
    fi

    if [[ "${OSMO_BASE_DOMAIN:-}" == "nip.io" && -n "${OSMO_INGRESS_HOSTNAME:-}" ]]; then
        return 0
    fi

    require_command kubectl
    require_command terraform

    if [[ "${OSMO_BASE_DOMAIN:-}" == "nip.io" ]]; then
        log "OSMO_BASE_DOMAIN=nip.io with no explicit hostname set; bootstrapping ingress-nginx to discover the public LoadBalancer IP"
    else
        [[ -n "${OSMO_INGRESS_HOSTNAME:-}" ]] || die "Could not derive OSMO_INGRESS_HOSTNAME for OSMO_BASE_DOMAIN=${OSMO_BASE_DOMAIN}. Source ./nebius-env-init.sh first or export OSMO_INGRESS_HOSTNAME explicitly."
        [[ -n "${KEYCLOAK_HOSTNAME:-}" ]] || die "Could not derive KEYCLOAK_HOSTNAME for OSMO_BASE_DOMAIN=${OSMO_BASE_DOMAIN}. Source ./nebius-env-init.sh first or export KEYCLOAK_HOSTNAME explicitly."
        log "OSMO_BASE_DOMAIN=${OSMO_BASE_DOMAIN}; bootstrapping ingress-nginx to discover the public LoadBalancer IP for DNS records"
    fi

    terraform -chdir="${SCRIPT_DIR}" init -input=false >/dev/null
    terraform -chdir="${SCRIPT_DIR}" apply \
      -auto-approve \
      -target=module.app.helm_release.ingress_nginx \
      -target=module.app.terraform_data.ingress_ready \
      -var "ingress_hostname=${bootstrap_ingress_hostname}" \
      -var "keycloak_hostname=${bootstrap_keycloak_hostname}"

    log "Waiting for ingress-nginx Service ${ingress_namespace}/${ingress_service_name} to receive a public IP"
    while [[ ${attempt} -lt 60 ]]; do
        lb_ip="$(
            KUBECONFIG="${SYNC_KUBECONFIG_PATH}" "${kubectl_cmd[@]}" \
              get svc -n "${ingress_namespace}" "${ingress_service_name}" \
              -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true
        )"

        if [[ -n "${lb_ip}" ]]; then
            break
        fi

        attempt=$((attempt + 1))
        sleep 10
    done

    [[ -n "${lb_ip}" ]] || die "Could not determine the ingress-nginx public IP from Service ${ingress_namespace}/${ingress_service_name}"
    PREPARE_INGRESS_PUBLIC_IP="${lb_ip}"
    export PREPARE_INGRESS_PUBLIC_IP

    if [[ "${OSMO_BASE_DOMAIN:-}" == "nip.io" ]]; then
        OSMO_INGRESS_HOSTNAME="osmo.${lb_ip}.nip.io"
        KEYCLOAK_HOSTNAME="${KEYCLOAK_HOSTNAME:-auth-osmo.${lb_ip}.nip.io}"
        export OSMO_INGRESS_HOSTNAME
        export KEYCLOAK_HOSTNAME

        log "Derived OSMO hostname: ${OSMO_INGRESS_HOSTNAME}"
        log "Derived Keycloak hostname: ${KEYCLOAK_HOSTNAME}"
        return 0
    fi

    log "Discovered ingress public IP: ${lb_ip}"
    log "OSMO hostname: ${OSMO_INGRESS_HOSTNAME}"
    log "Keycloak hostname: ${KEYCLOAK_HOSTNAME}"
    prepare_upsert_public_dns_records "${lb_ip}"
}

run_sync_kubeconfig() {
    local arg=""

    SYNC_INFRA_DIR="${SCRIPT_DIR}/infra"
    SYNC_KUBECONFIG_PATH="${SCRIPT_DIR}/generated/kubeconfig"
    SYNC_WRITE_ENV_PATH="${SCRIPT_DIR}/cluster-access.env"
    SYNC_CONTEXT_NAME=""
    SYNC_ENDPOINT_MODE=""

    while [[ $# -gt 0 ]]; do
        arg="$1"
        case "${arg}" in
            --infra-dir)
                [[ $# -ge 2 ]] || die "--infra-dir requires a value"
                SYNC_INFRA_DIR="$2"
                shift 2
                ;;
            --kubeconfig)
                [[ $# -ge 2 ]] || die "--kubeconfig requires a value"
                SYNC_KUBECONFIG_PATH="$2"
                shift 2
                ;;
            --context-name)
                [[ $# -ge 2 ]] || die "--context-name requires a value"
                SYNC_CONTEXT_NAME="$2"
                shift 2
                ;;
            --write-env)
                [[ $# -ge 2 ]] || die "--write-env requires a value"
                SYNC_WRITE_ENV_PATH="$2"
                shift 2
                ;;
            --external)
                SYNC_ENDPOINT_MODE="external"
                shift
                ;;
            --internal)
                SYNC_ENDPOINT_MODE="internal"
                shift
                ;;
            -h|--help)
                usage_sync_kubeconfig
                return 0
                ;;
            *)
                die "Unknown argument for sync-kubeconfig: ${arg}"
                ;;
        esac
    done

    load_local_env_init

    require_command terraform
    require_command nebius
    require_command jq

    [[ -d "${SYNC_INFRA_DIR}" ]] || die "Infra Terraform root not found at ${SYNC_INFRA_DIR}"

    SYNC_CLUSTER_ID="$(terraform_output_json_value "${SYNC_INFRA_DIR}" "cluster_id" || true)"
    SYNC_CLUSTER_NAME="$(terraform_output_json_value "${SYNC_INFRA_DIR}" "cluster_name" || true)"
    SYNC_CLUSTER_ENDPOINT="$(terraform_output_json_value "${SYNC_INFRA_DIR}" "cluster_endpoint" || true)"

    [[ "${SYNC_CLUSTER_ID}" =~ ^mk8scluster-[a-z0-9]+$ ]] || die "Could not read a valid cluster_id from ${SYNC_INFRA_DIR}. Run terraform apply in ${SYNC_INFRA_DIR} first."

    if [[ -z "${SYNC_CONTEXT_NAME}" ]]; then
        SYNC_CONTEXT_NAME="${SYNC_CLUSTER_NAME:-osmo-${NEBIUS_PROJECT_ID#project-}}"
    fi

    sync_infer_endpoint_mode "${SYNC_CLUSTER_ENDPOINT}"

    mkdir -p "$(dirname "${SYNC_KUBECONFIG_PATH}")"

    log "Writing kubeconfig for cluster ${SYNC_CLUSTER_ID} (${SYNC_ENDPOINT_MODE} endpoint)"
    if [[ "${SYNC_ENDPOINT_MODE}" == "external" ]]; then
        nebius mk8s cluster get-credentials \
          --id "${SYNC_CLUSTER_ID}" \
          --kubeconfig "${SYNC_KUBECONFIG_PATH}" \
          --context-name "${SYNC_CONTEXT_NAME}" \
          --external \
          --force >/dev/null
    else
        nebius mk8s cluster get-credentials \
          --id "${SYNC_CLUSTER_ID}" \
          --kubeconfig "${SYNC_KUBECONFIG_PATH}" \
          --context-name "${SYNC_CONTEXT_NAME}" \
          --internal \
          --force >/dev/null
    fi

    sync_write_env_file
}

usage_register_oidc() {
    cat <<'EOF'
Usage:
  ./prepare-app-prereqs.sh register-oidc [options]

Options:
  --auth-domain DOMAIN     Explicit Keycloak hostname to use for the redirect URI.
  --project-id ID          Nebius project id. Defaults to NEBIUS_PROJECT_ID.
  --client-name NAME       OIDC client name. Defaults to keycloak-osmo.
  --client-id ID           Existing OIDC client id. If omitted, resolve by name or create.
  --issuer-url URL         OIDC issuer for deployment output. Defaults to https://auth.nebius.com.
  --write-env PATH         Write an export snippet to PATH. Defaults to ./osmo-sso.env.
  -h, --help               Show this help.
EOF
}

run_register_oidc() {
    local arg=""
    local client_list_json=""
    local client_json=""
    local create_out=""
    local secret_out=""
    local tmp_yaml=""
    REGISTER_AUTH_DOMAIN="${AUTH_DOMAIN:-}"
    REGISTER_PROJECT_ID="${PROJECT_ID:-${NEBIUS_PROJECT_ID:-}}"
    REGISTER_CLIENT_NAME="${CLIENT_NAME:-keycloak-osmo}"
    REGISTER_CLIENT_ID="${CLIENT_ID:-${NEBIUS_SSO_CLIENT_ID:-}}"
    REGISTER_ISSUER_URL="${ISSUER_URL:-${NEBIUS_SSO_ISSUER_URL:-https://auth.nebius.com}}"
    REGISTER_WRITE_ENV_PATH="${WRITE_ENV_PATH:-${SCRIPT_DIR}/osmo-sso.env}"

    while [[ $# -gt 0 ]]; do
        arg="$1"
        case "${arg}" in
            --auth-domain)
                [[ $# -ge 2 ]] || die "--auth-domain requires a value"
                REGISTER_AUTH_DOMAIN="$2"
                shift 2
                ;;
            --project-id)
                [[ $# -ge 2 ]] || die "--project-id requires a value"
                REGISTER_PROJECT_ID="$2"
                shift 2
                ;;
            --client-name)
                [[ $# -ge 2 ]] || die "--client-name requires a value"
                REGISTER_CLIENT_NAME="$2"
                shift 2
                ;;
            --client-id)
                [[ $# -ge 2 ]] || die "--client-id requires a value"
                REGISTER_CLIENT_ID="$2"
                shift 2
                ;;
            --issuer-url)
                [[ $# -ge 2 ]] || die "--issuer-url requires a value"
                REGISTER_ISSUER_URL="$2"
                shift 2
                ;;
            --write-env)
                [[ $# -ge 2 ]] || die "--write-env requires a value"
                REGISTER_WRITE_ENV_PATH="$2"
                shift 2
                ;;
            -h|--help)
                usage_register_oidc
                return 0
                ;;
            *)
                die "Unknown argument for register-oidc: ${arg}"
                ;;
        esac
    done

    load_local_env_init
    require_command npc
    require_command jq

    if [[ -z "${REGISTER_AUTH_DOMAIN}" ]]; then
        if [[ -n "${KEYCLOAK_HOSTNAME:-}" ]]; then
            REGISTER_AUTH_DOMAIN="${KEYCLOAK_HOSTNAME}"
        elif [[ -n "${OSMO_INGRESS_HOSTNAME:-}" ]]; then
            REGISTER_AUTH_DOMAIN="auth-${OSMO_INGRESS_HOSTNAME}"
        else
            die "Set KEYCLOAK_HOSTNAME or OSMO_INGRESS_HOSTNAME in ./nebius-env-init.sh, source that file, or pass --auth-domain explicitly."
        fi
    fi

    [[ -n "${REGISTER_PROJECT_ID}" ]] || die "Set NEBIUS_PROJECT_ID in ./nebius-env-init.sh, source that file, or pass --project-id."

    REGISTER_REDIRECT_URI="https://${REGISTER_AUTH_DOMAIN}/realms/osmo/broker/nebius-sso/endpoint"
    REGISTER_CLIENT_SECRET=""
    REGISTER_CLIENT_STATE="existing"

    log "Project id: ${REGISTER_PROJECT_ID}"
    log "Keycloak hostname: ${REGISTER_AUTH_DOMAIN}"
    log "Redirect URI: ${REGISTER_REDIRECT_URI}"

    if [[ -z "${REGISTER_CLIENT_ID}" ]]; then
        log "Resolving OIDC client '${REGISTER_CLIENT_NAME}' by name..."
        client_list_json=$(npc_run_json "Failed to list OIDC clients in project ${REGISTER_PROJECT_ID}" \
            iam oidc-client list --parent-id "${REGISTER_PROJECT_ID}" --all)
        REGISTER_CLIENT_ID=$(json_find_client_id_by_name "${client_list_json}" "${REGISTER_CLIENT_NAME}")
    fi

    if [[ -z "${REGISTER_CLIENT_ID}" ]]; then
        REGISTER_CLIENT_STATE="created"
        log "Creating OIDC client '${REGISTER_CLIENT_NAME}'..."

        tmp_yaml="$(make_temp_file "oidc-client" ".yaml")"
        cat >"${tmp_yaml}" <<EOF
metadata:
  parent_id: "${REGISTER_PROJECT_ID}"
  name: "${REGISTER_CLIENT_NAME}"
spec:
  client_authentication_methods:
    - client_secret_basic
  redirect_uris:
    - "${REGISTER_REDIRECT_URI}"
  pkce_enabled: true
  scopes:
    - openid
  session_management_enabled: false
  authorization_grant_types:
    - authorization_code
EOF

        create_out=$(npc_run_json "Failed to create OIDC client '${REGISTER_CLIENT_NAME}'" \
            iam oidc-client create --file "${tmp_yaml}")
        rm -f "${tmp_yaml}"
        REGISTER_CLIENT_ID=$(json_find_client_id "${create_out}")
        [[ -n "${REGISTER_CLIENT_ID}" ]] || die "Failed to create OIDC client. npc output: ${create_out}"
    else
        log "Using existing OIDC client: ${REGISTER_CLIENT_ID}"
    fi

    log "Updating redirect URI on client ${REGISTER_CLIENT_ID}..."
    npc_run_json "Failed to update redirect URI on client ${REGISTER_CLIENT_ID}" \
        iam oidc-client update "${REGISTER_CLIENT_ID}" \
        --redirect-uris "${REGISTER_REDIRECT_URI}" \
        --authorization-grant-types authorization_code \
        --patch \
        >/dev/null

    client_json=$(npc_run_json "Failed to fetch OIDC client ${REGISTER_CLIENT_ID} after update" \
        iam oidc-client get --id "${REGISTER_CLIENT_ID}")
    json_client_has_redirect_uri "${client_json}" "${REGISTER_REDIRECT_URI}" || \
        die "OIDC client ${REGISTER_CLIENT_ID} is missing redirect URI ${REGISTER_REDIRECT_URI} after update"

    log "Force-replacing client secret..."
    # GenerateClientSecret lacks content-based routing (unlike Get/Update/Create/List),
    # so it hits the default IAM backend (MAN) and returns NotFound for clients in
    # other regional backends (e.g. uk-south1). Force routing via the resource-ID
    # prefix derived from the project id (e.g. project-e03... -> e03).
    REGISTER_ROUTING_CODE="${REGISTER_PROJECT_ID#*-}"
    REGISTER_ROUTING_CODE="${REGISTER_ROUTING_CODE:0:3}"
    secret_out=$(npc_run_json "Failed to rotate client secret for ${REGISTER_CLIENT_ID}" \
        iam oidc-client generate-client-secret --client-id "${REGISTER_CLIENT_ID}" --force-replace --force-routing-code "${REGISTER_ROUTING_CODE}")
    REGISTER_CLIENT_SECRET=$(json_find_client_secret "${secret_out}")
    [[ -n "${REGISTER_CLIENT_SECRET}" ]] || die "Failed to parse client secret. npc output: ${secret_out}"

    {
        printf '# Generated by %s register-oidc\n' "${SCRIPT_DIR}/prepare-app-prereqs.sh"
        printf '# Redirect URI: %s\n' "${REGISTER_REDIRECT_URI}"
        if [[ -n "${OSMO_INGRESS_HOSTNAME:-}" ]]; then
            write_export_line "OSMO_INGRESS_HOSTNAME" "${OSMO_INGRESS_HOSTNAME}"
            write_export_line "TF_VAR_ingress_hostname" "${OSMO_INGRESS_HOSTNAME}"
        fi
        write_export_line "KEYCLOAK_HOSTNAME" "${REGISTER_AUTH_DOMAIN}"
        write_export_line "NEBIUS_SSO_ENABLED" "true"
        write_export_line "NEBIUS_SSO_ISSUER_URL" "${REGISTER_ISSUER_URL}"
        write_export_line "NEBIUS_SSO_CLIENT_ID" "${REGISTER_CLIENT_ID}"
        write_export_line "NEBIUS_SSO_CLIENT_SECRET" "${REGISTER_CLIENT_SECRET}"
        write_export_line "TF_VAR_keycloak_hostname" "${REGISTER_AUTH_DOMAIN}"
        write_export_line "TF_VAR_nebius_sso_enabled" "true"
        write_export_line "TF_VAR_nebius_sso_issuer_url" "${REGISTER_ISSUER_URL}"
        write_export_line "TF_VAR_nebius_sso_client_id" "${REGISTER_CLIENT_ID}"
        write_export_line "TF_VAR_nebius_sso_client_secret" "${REGISTER_CLIENT_SECRET}"
    } | write_file_atomically "${REGISTER_WRITE_ENV_PATH}"
    log "Wrote env snippet to ${REGISTER_WRITE_ENV_PATH}"

    cat <<EOF

OIDC client is ready.

Client state: ${REGISTER_CLIENT_STATE}
Client id:    ${REGISTER_CLIENT_ID}
Redirect URI: ${REGISTER_REDIRECT_URI}
Issuer URL:   ${REGISTER_ISSUER_URL}

Next:
  source ${REGISTER_WRITE_ENV_PATH}
  terraform init
  terraform apply

Note:
  Rerunning this command force-replaces the OIDC client secret.
  If you rerun step 4, source the new env file and rerun step 5.
EOF
}

usage_prepare() {
    cat <<'EOF'
Usage:
  ./prepare-app-prereqs.sh [prepare] [options]

Options:
  --project-id ID            Nebius project id. Defaults to NEBIUS_PROJECT_ID.
  --write-sso-env PATH       Defaults to ./osmo-sso.env.
  --write-cluster-env PATH   Defaults to ./cluster-access.env.
  -h, --help                 Show this help.

When OSMO_BASE_DOMAIN=nip.io and no explicit hostnames are set, this command
will:
  1. materialize kubeconfig
  2. bootstrap ingress-nginx with a targeted Terraform apply
  3. read the ingress public IP
  4. derive osmo.<ip>.nip.io and auth-osmo.<ip>.nip.io
  5. register Nebius SSO using those final hostnames when npc is available,
     otherwise disable Nebius SSO and keep the auth-enabled Keycloak
     breakglass-user path
  6. resolve storage/app secret overrides for the final terraform apply

When OSMO_BASE_DOMAIN points at a real DNS zone, this command will:
  1. materialize kubeconfig
  2. bootstrap ingress-nginx with a targeted Terraform apply
  3. read the ingress public IP
  4. upsert the public A records for OSMO and Keycloak when DNS_ZONE_ID and
     DNS_NPC_PROFILE are configured, otherwise print the needed records
  5. register Nebius SSO using the stable derived hostnames
  6. resolve storage/app secret overrides for the final terraform apply
EOF
}

usage_cleanup_dns() {
    cat <<'EOF'
Usage:
  ./prepare-app-prereqs.sh cleanup-dns

Deletes the managed public DNS A recordsets for the current OSMO and Keycloak
hostnames. This is useful if you ran step 4 and want to clean up DNS before a
successful app Terraform state exists.
EOF
}

run_cleanup_dns() {
    load_local_env_init

    [[ -n "${OSMO_BASE_DOMAIN:-}" ]] || die "OSMO_BASE_DOMAIN is empty. Source ./nebius-env-init.sh first."
    [[ -n "${OSMO_INGRESS_HOSTNAME:-}" ]] || die "OSMO_INGRESS_HOSTNAME is empty. Source ./nebius-env-init.sh first."
    [[ -n "${KEYCLOAK_HOSTNAME:-}" ]] || die "KEYCLOAK_HOSTNAME is empty. Source ./nebius-env-init.sh first."

    if [[ "${OSMO_BASE_DOMAIN:-}" == "nip.io" ]]; then
        log "OSMO_BASE_DOMAIN=nip.io; there are no managed public DNS records to delete"
        return 0
    fi

    if [[ -z "${DNS_ZONE_ID:-}" || -z "${DNS_NPC_PROFILE:-}" ]]; then
        die "DNS cleanup requires both DNS_ZONE_ID and DNS_NPC_PROFILE to be set."
    fi

    ACTION="delete" \
    OSMO_BASE_DOMAIN="${OSMO_BASE_DOMAIN}" \
    OSMO_HOSTNAME="${OSMO_INGRESS_HOSTNAME}" \
    KEYCLOAK_HOSTNAME="${KEYCLOAK_HOSTNAME}" \
    DNS_NPC_PROFILE="${DNS_NPC_PROFILE}" \
    DNS_ZONE_ID="${DNS_ZONE_ID}" \
    /bin/bash "${SCRIPT_DIR}/scripts/manage-public-dns-records.sh"
}

run_prepare() {
    local arg=""
    local prepare_project_id=""
    local prepare_sso_env="${SCRIPT_DIR}/osmo-sso.env"
    local prepare_cluster_env="${SCRIPT_DIR}/cluster-access.env"
    local prepare_nipio_no_npc="false"

    while [[ $# -gt 0 ]]; do
        arg="$1"
        case "${arg}" in
            --project-id)
                [[ $# -ge 2 ]] || die "--project-id requires a value"
                prepare_project_id="$2"
                shift 2
                ;;
            --write-sso-env)
                [[ $# -ge 2 ]] || die "--write-sso-env requires a value"
                prepare_sso_env="$2"
                shift 2
                ;;
            --write-cluster-env)
                [[ $# -ge 2 ]] || die "--write-cluster-env requires a value"
                prepare_cluster_env="$2"
                shift 2
                ;;
            -h|--help)
                usage_prepare
                return 0
                ;;
            *)
                die "Unknown argument for prepare: ${arg}"
                ;;
        esac
    done

    load_local_env_init
    prepare_project_id="${prepare_project_id:-${NEBIUS_PROJECT_ID:-}}"
    [[ -n "${prepare_project_id}" ]] || die "NEBIUS_PROJECT_ID is empty. Source ./nebius-env-init.sh or pass --project-id."

    run_sync_kubeconfig --write-env "${prepare_cluster_env}"
    prepare_bootstrap_ingress
    if command -v npc >/dev/null 2>&1; then
        run_register_oidc --project-id "${prepare_project_id}" --write-env "${prepare_sso_env}"
    elif [[ "${OSMO_BASE_DOMAIN:-}" == "nip.io" ]]; then
        warn "npc not found; using the nip.io fallback and disabling Nebius SSO for this deployment"
        write_prepare_nipio_no_npc_env "${prepare_sso_env}"
        prepare_nipio_no_npc="true"
    else
        die "npc is required for OSMO_BASE_DOMAIN=${OSMO_BASE_DOMAIN:-<unset>}. Install npc or set OSMO_BASE_DOMAIN=nip.io for the no-npc fallback."
    fi
    write_prepare_tf_secret_overrides "${prepare_sso_env}"

    echo ""
    if [[ "${OSMO_BASE_DOMAIN:-}" != "nip.io" && -n "${PREPARE_INGRESS_PUBLIC_IP:-}" && ( -z "${DNS_ZONE_ID:-}" || -z "${DNS_NPC_PROFILE:-}" ) ]]; then
        echo "Before the final app apply, ensure these public DNS records resolve to ${PREPARE_INGRESS_PUBLIC_IP}:"
        echo "  ${OSMO_INGRESS_HOSTNAME}"
        echo "  ${KEYCLOAK_HOSTNAME}"
        echo ""
    fi
    if [[ "${prepare_nipio_no_npc}" == "true" ]]; then
        echo "Nebius SSO was skipped because npc is not installed and OSMO_BASE_DOMAIN=nip.io."
        echo "The final app apply will keep auth and TLS enabled with local Keycloak login."
        echo ""
    fi
    echo "Next:"
    echo "  source ${prepare_sso_env}"
    echo "  terraform init"
    echo "  terraform apply"
}

COMMAND="prepare"
if [[ $# -gt 0 ]]; then
    case "$1" in
        prepare|cleanup-dns|sync-kubeconfig|register-oidc)
            COMMAND="$1"
            shift
            ;;
        -h|--help)
            usage_main
            exit 0
            ;;
    esac
fi

case "${COMMAND}" in
    prepare)
        run_prepare "$@"
        ;;
    cleanup-dns)
        run_cleanup_dns "$@"
        ;;
    sync-kubeconfig)
        run_sync_kubeconfig "$@"
        ;;
    register-oidc)
        run_register_oidc "$@"
        ;;
    *)
        usage_main
        exit 1
        ;;
esac
