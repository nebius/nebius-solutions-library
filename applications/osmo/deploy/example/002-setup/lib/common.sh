#!/bin/bash
#
# Common functions for setup scripts
#

# Colors
export RED='\033[0;31m'
export GREEN='\033[0;32m'
export YELLOW='\033[1;33m'
export BLUE='\033[0;34m'
export NC='\033[0m'

# Logging functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[✓]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[!]${NC} $1"
}

log_error() {
    echo -e "${RED}[✗]${NC} $1"
}

# Read input with a prompt into a variable (bash/zsh compatible).
read_prompt_var() {
    local prompt=$1
    local var_name=$2
    local default=$3
    local value=""
    local read_from="/dev/tty"
    local write_to="/dev/tty"

    if [[ ! -r "/dev/tty" || ! -w "/dev/tty" ]]; then
        read_from="/dev/stdin"
        write_to="/dev/stdout"
    fi

    if [[ -n "$default" ]]; then
        printf "%s [%s]: " "$prompt" "$default" >"$write_to"
    else
        printf "%s: " "$prompt" >"$write_to"
    fi

    IFS= read -r value <"$read_from"
    if [[ -z "$value" && -n "$default" ]]; then
        value="$default"
    fi

    eval "$var_name='$value'"
}

# Read a secret value into a variable (no echo).
read_secret_var() {
    local prompt=$1
    local var_name=$2
    local value=""
    local read_from="/dev/tty"
    local write_to="/dev/tty"

    if [[ ! -r "/dev/tty" || ! -w "/dev/tty" ]]; then
        read_from="/dev/stdin"
        write_to="/dev/stdout"
    fi

    printf "%s: " "$prompt" >"$write_to"
    stty -echo <"$read_from"
    IFS= read -r value <"$read_from"
    stty echo <"$read_from"
    printf "\n" >"$write_to"

    eval "$var_name='$value'"
}

# Check if command exists
check_command() {
    command -v "$1" &>/dev/null
}

kubectl_real() {
    local kubectl_bin
    kubectl_bin=$(type -P kubectl 2>/dev/null || true)
    if [[ -z "$kubectl_bin" ]]; then
        echo "kubectl binary not found" >&2
        return 127
    fi
    "$kubectl_bin" "$@"
}

# Shared kubectl wrapper for setup scripts.
# For `kubectl apply`, disable schema validation and capture stdin-backed
# manifests to a temp file so retries survive transient IAM / OpenAPI failures.
kubectl() {
    if [[ "${1:-}" != "apply" ]]; then
        kubectl_real "$@"
        return $?
    fi

    shift
    local args=()
    local tmp_file=""
    local captured_stdin=false

    while [[ $# -gt 0 ]]; do
        if [[ "$1" == "-f" && "${2:-}" == "-" ]]; then
            if [[ "$captured_stdin" == "false" ]]; then
                tmp_file=$(mktemp "/tmp/kubectl-apply.XXXXXX.yaml")
                cat >"$tmp_file"
                captured_stdin=true
            fi
            args+=("-f" "$tmp_file")
            shift 2
            continue
        fi
        args+=("$1")
        shift
    done

    if [[ "$captured_stdin" == "true" ]]; then
        retry_with_backoff 3 2 10 kubectl_real apply --validate=false "${args[@]}"
    else
        retry_with_backoff 3 2 10 kubectl_real apply --validate=false "${args[@]}"
    fi
    local rc=$?

    if [[ -n "$tmp_file" ]]; then
        rm -f "$tmp_file" 2>/dev/null || true
    fi

    return $rc
}

# Check whether a local TCP port is already listening.
is_local_port_in_use() {
    local port="$1"

    if check_command lsof; then
        lsof -nP -iTCP:"$port" -sTCP:LISTEN &>/dev/null
    elif check_command ss; then
        ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]${port}$"
    elif check_command netstat; then
        netstat -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]${port}$"
    else
        return 1
    fi
}

# Track script-owned port-forwards so a later rerun can clean them up and
# reclaim the preferred local port.
port_forward_state_file() {
    local port="$1"
    echo "/tmp/osmo-port-forward.${port}.state"
}

write_port_forward_state() {
    local port="$1"
    local pf_pid="$2"
    local log_file="$3"
    local ns="$4"
    local resource="$5"
    local remote_port="$6"
    local description="$7"
    local state_file

    state_file=$(port_forward_state_file "$port")
    cat >"$state_file" <<EOF
pid=${pf_pid}
log=${log_file}
namespace=${ns}
resource=${resource}
remote_port=${remote_port}
description=${description}
EOF
}

remove_port_forward_state() {
    local pf_pid="${1:-}"
    local log_file="${2:-}"
    local local_port="${3:-}"
    local state_file
    local state_pid
    local state_log

    if [[ -n "$local_port" ]]; then
        rm -f "$(port_forward_state_file "$local_port")" 2>/dev/null || true
        return 0
    fi

    for state_file in /tmp/osmo-port-forward.*.state; do
        [[ -e "$state_file" ]] || break
        state_pid=$(awk -F= '$1=="pid" {print substr($0, index($0, "=") + 1); exit}' "$state_file" 2>/dev/null)
        state_log=$(awk -F= '$1=="log" {print substr($0, index($0, "=") + 1); exit}' "$state_file" 2>/dev/null)
        if [[ -n "$pf_pid" && "$state_pid" == "$pf_pid" ]]; then
            rm -f "$state_file" 2>/dev/null || true
            break
        fi
        if [[ -n "$log_file" && "$state_log" == "$log_file" ]]; then
            rm -f "$state_file" 2>/dev/null || true
            break
        fi
    done
}

cleanup_script_owned_port_forward() {
    local local_port="$1"
    local state_file
    local pf_pid
    local log_file
    local description
    local cmd

    state_file=$(port_forward_state_file "$local_port")
    [[ -f "$state_file" ]] || return 1

    pf_pid=$(awk -F= '$1=="pid" {print substr($0, index($0, "=") + 1); exit}' "$state_file" 2>/dev/null)
    log_file=$(awk -F= '$1=="log" {print substr($0, index($0, "=") + 1); exit}' "$state_file" 2>/dev/null)
    description=$(awk -F= '$1=="description" {print substr($0, index($0, "=") + 1); exit}' "$state_file" 2>/dev/null)

    if [[ -z "$pf_pid" ]]; then
        rm -f "$state_file" 2>/dev/null || true
        return 1
    fi

    if ! kill -0 "$pf_pid" 2>/dev/null; then
        stop_port_forward "$pf_pid" "$log_file" "$local_port"
        return 0
    fi

    cmd=$(ps -p "$pf_pid" -o command= 2>/dev/null || true)
    if [[ "$cmd" != *"kubectl port-forward"* ]]; then
        rm -f "$state_file" 2>/dev/null || true
        return 1
    fi

    log_warning "Cleaning up stale script-owned port-forward on local port ${local_port}${description:+ (${description})}..."
    stop_port_forward "$pf_pid" "$log_file" "$local_port"
    sleep 1
    return 0
}

# Show the tail of a captured kubectl port-forward log.
show_port_forward_log() {
    local log_file="$1"
    [[ -f "$log_file" ]] || return 0
    tail -n 20 "$log_file" 2>/dev/null | sed 's/^/  /'
}

# Start a kubectl port-forward on the first available local port.
# Sets PORT_FORWARD_PID, PORT_FORWARD_PORT, and PORT_FORWARD_LOG.
start_kubectl_port_forward() {
    local ns="$1"
    local resource="$2"
    local remote_port="$3"
    local preferred_local_port="${4:-8080}"
    local description="${5:-${resource}:${remote_port}}"
    local max_port_tries="${6:-20}"
    local log_file
    local local_port
    local pf_pid
    local attempt

    log_file=$(mktemp "/tmp/osmo-port-forward.XXXXXX")

    for attempt in $(seq 0 "$max_port_tries"); do
        local_port=$((preferred_local_port + attempt))

        if is_local_port_in_use "$local_port"; then
            cleanup_script_owned_port_forward "$local_port" >/dev/null 2>&1 || true
        fi

        if is_local_port_in_use "$local_port"; then
            if [[ "$attempt" -eq 0 ]]; then
                log_warning "Local port ${local_port} is already in use; trying another port for ${description}..."
            fi
            continue
        fi

        kubectl port-forward -n "$ns" "$resource" "${local_port}:${remote_port}" >"$log_file" 2>&1 &
        pf_pid=$!
        sleep 1

        if kill -0 "$pf_pid" 2>/dev/null; then
            PORT_FORWARD_PID=$pf_pid
            PORT_FORWARD_PORT=$local_port
            PORT_FORWARD_LOG=$log_file
            export PORT_FORWARD_PID PORT_FORWARD_PORT PORT_FORWARD_LOG
            write_port_forward_state "$local_port" "$pf_pid" "$log_file" "$ns" "$resource" "$remote_port" "$description"

            if [[ "$local_port" != "$preferred_local_port" ]]; then
                log_warning "Using local port ${local_port} for ${description} (preferred port ${preferred_local_port} was unavailable)"
            fi
            return 0
        fi

        wait "$pf_pid" 2>/dev/null || true
        if grep -qiE 'address already in use|bind:.*in use|unable to listen on port' "$log_file" 2>/dev/null; then
            if [[ "$attempt" -eq 0 ]]; then
                log_warning "kubectl could not bind local port ${local_port}; trying another port for ${description}..."
            fi
            continue
        fi

        log_error "Failed to start port-forward for ${description}"
        show_port_forward_log "$log_file"
        rm -f "$log_file" 2>/dev/null || true
        return 1
    done

    log_error "Could not find an available local port for ${description} starting at ${preferred_local_port}"
    rm -f "$log_file" 2>/dev/null || true
    return 1
}

# Stop a kubectl port-forward and clean up its log file.
stop_port_forward() {
    local pf_pid="${1:-${PORT_FORWARD_PID:-}}"
    local log_file="${2:-${PORT_FORWARD_LOG:-}}"
    local local_port="${3:-${PORT_FORWARD_PORT:-}}"

    if [[ -n "$pf_pid" ]]; then
        kill "$pf_pid" 2>/dev/null || true
        wait "$pf_pid" 2>/dev/null || true
    fi

    if [[ -n "$log_file" ]]; then
        rm -f "$log_file" 2>/dev/null || true
    fi

    remove_port_forward_state "$pf_pid" "$log_file" "$local_port"
}

# Return the HTTP status for a URL with bounded timeouts.
http_status() {
    local url="$1"

    curl -sS \
        --connect-timeout "${OSMO_CURL_CONNECT_TIMEOUT:-5}" \
        --max-time "${OSMO_READY_CURL_MAX_TIME:-5}" \
        -o /dev/null \
        -w "%{http_code}" \
        "$url" 2>/dev/null || echo "000"
}

# Wait for an HTTP endpoint to return a ready status code.
wait_for_http_ready() {
    local url="$1"
    local timeout="${2:-30}"
    local description="${3:-$url}"
    local elapsed=0
    local status="000"

    while [[ "$elapsed" -lt "$timeout" ]]; do
        status=$(http_status "$url")
        if [[ "$status" =~ ^(200|401|403)$ ]]; then
            return 0
        fi

        if [[ -n "${PORT_FORWARD_PID:-}" ]]; then
            if ! kill -0 "${PORT_FORWARD_PID}" 2>/dev/null; then
                log_error "Port-forward exited while waiting for ${description}"
                show_port_forward_log "${PORT_FORWARD_LOG:-}"
                return 1
            fi
        fi

        sleep 1
        ((elapsed += 1))
    done

    log_error "Timed out waiting for ${description} (last HTTP status: ${status})"
    if [[ -n "${PORT_FORWARD_LOG:-}" ]]; then
        show_port_forward_log "${PORT_FORWARD_LOG}"
    fi
    return 1
}

# Retry with exponential backoff
retry_with_backoff() {
    local max_attempts=${1:-5}
    local delay=${2:-2}
    local max_delay=${3:-60}
    shift 3
    local cmd=("$@")
    
    local attempt=1
    while [[ $attempt -le $max_attempts ]]; do
        log_info "Attempt $attempt/$max_attempts: ${cmd[*]}"
        if "${cmd[@]}"; then
            return 0
        fi
        
        if [[ $attempt -lt $max_attempts ]]; then
            log_warning "Failed, retrying in ${delay}s..."
            sleep "$delay"
            delay=$((delay * 2))
            if [[ $delay -gt $max_delay ]]; then
                delay=$max_delay
            fi
        fi
        ((attempt++))
    done
    
    log_error "All $max_attempts attempts failed"
    return 1
}

# Wait for a condition with timeout
wait_for_condition() {
    local description=$1
    local timeout=${2:-300}
    local interval=${3:-10}
    shift 3
    local cmd=("$@")
    
    log_info "Waiting for $description (timeout: ${timeout}s)..."
    
    local elapsed=0
    while [[ $elapsed -lt $timeout ]]; do
        if "${cmd[@]}" &>/dev/null; then
            log_success "$description"
            return 0
        fi
        sleep "$interval"
        ((elapsed += interval))
        echo -n "."
    done
    
    echo ""
    log_error "Timeout waiting for $description"
    return 1
}

# Check kubectl connection and verify we're targeting the correct cluster
check_kubectl() {
    if ! check_command kubectl; then
        log_error "kubectl not found"
        return 1
    fi

    if ! retry_with_backoff 3 2 10 kubectl cluster-info &>/dev/null; then
        log_error "Cannot connect to Kubernetes cluster"
        return 1
    fi

    # Verify current context matches the expected cluster from Terraform
    local expected_cluster
    expected_cluster=$(get_tf_output "cluster_name" "../001-iac" 2>/dev/null || true)
    if [[ -n "$expected_cluster" ]]; then
        local current_context
        current_context=$(kubectl config current-context 2>/dev/null || true)
        if [[ -n "$current_context" && "$current_context" != *"$expected_cluster"* ]]; then
            log_error "Wrong Kubernetes context!"
            log_error "  Current context: $current_context"
            log_error "  Expected cluster: $expected_cluster"
            log_info "Switch context with: nebius mk8s cluster get-credentials --id \$(terraform -chdir=../001-iac output -raw cluster_id) --external"
            return 1
        fi
        log_success "kubectl connected to cluster ($expected_cluster)"
    else
        log_success "kubectl connected to cluster"
    fi
    return 0
}

# Check Helm
check_helm() {
    if ! check_command helm; then
        log_error "helm not found"
        return 1
    fi
    
    log_success "helm available"
    return 0
}

# Install Helm chart with retry
helm_install() {
    local name=$1
    local chart=$2
    local namespace=$3
    shift 3
    local extra_args=("$@")
    
    log_info "Installing Helm chart: $name"
    
    kubectl create namespace "$namespace" --dry-run=client -o yaml | kubectl apply -f -
    
    retry_with_backoff 3 5 30 helm upgrade --install "$name" "$chart" \
        --namespace "$namespace" \
        --wait --timeout 10m \
        "${extra_args[@]}"
}

# Wait for pods to be ready
wait_for_pods() {
    local namespace=$1
    local label_selector=$2
    local timeout=${3:-300}
    
    wait_for_condition "pods with label $label_selector in $namespace" \
        "$timeout" 10 \
        kubectl wait --for=condition=Ready pods \
            -n "$namespace" \
            -l "$label_selector" \
            --timeout=10s
}

# Detect OSMO service URL from the NGINX Ingress Controller's LoadBalancer.
#
# When OSMO_TLS_ENABLED=true and OSMO_INGRESS_HOSTNAME is set, returns
# https://<hostname>. Otherwise falls back to http://<ip>.
#
# Lookup order:
#   0. If TLS enabled + hostname set, return https://<hostname> immediately
#   1. LoadBalancer external IP   (cloud assigns a public/internal IP)
#   2. LoadBalancer hostname       (some clouds return a DNS name instead)
#   3. Controller ClusterIP        (fallback – works from inside the cluster)
#
# Usage:
#   url=$(detect_service_url)
#   [[ -n "$url" ]] && echo "OSMO reachable at $url"
detect_service_url() {
    local ns="${INGRESS_NAMESPACE:-ingress-nginx}"
    local tls_enabled="${OSMO_TLS_ENABLED:-false}"
    local hostname="${OSMO_INGRESS_HOSTNAME:-}"
    local scheme="http"

    if [[ "$tls_enabled" == "true" ]]; then
        scheme="https"
        # If hostname is configured, prefer it (TLS certs are issued for the domain)
        if [[ -n "$hostname" ]]; then
            echo "${scheme}://${hostname}"
            return 0
        fi
    fi

    # Find the controller service (works for the community ingress-nginx chart)
    local lb_ip lb_host cluster_ip svc_name
    svc_name=$(kubectl get svc -n "$ns" \
        -l app.kubernetes.io/name=ingress-nginx,app.kubernetes.io/component=controller \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)

    if [[ -n "$svc_name" ]]; then
        # 1. LoadBalancer IP
        lb_ip=$(kubectl get svc "$svc_name" -n "$ns" \
            -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
        if [[ -n "$lb_ip" ]]; then
            echo "${scheme}://${lb_ip}"
            return 0
        fi

        # 2. LoadBalancer hostname (e.g. ELB on AWS)
        lb_host=$(kubectl get svc "$svc_name" -n "$ns" \
            -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)
        if [[ -n "$lb_host" ]]; then
            echo "${scheme}://${lb_host}"
            return 0
        fi

        # 3. ClusterIP of the controller
        cluster_ip=$(kubectl get svc "$svc_name" -n "$ns" \
            -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)
        if [[ -n "$cluster_ip" && "$cluster_ip" != "None" ]]; then
            echo "${scheme}://${cluster_ip}"
            return 0
        fi
    fi

    # Nothing found
    return 1
}

# Get Terraform output (supports nested values like "postgresql.host")
get_tf_output() {
    local name=$1
    local tf_dir=${2:-../001-iac}
    
    # Check if name contains a dot (nested value)
    if [[ "$name" == *.* ]]; then
        local base_name="${name%%.*}"
        local key="${name#*.}"
        terraform -chdir="$tf_dir" output -json "$base_name" 2>/dev/null | jq -r ".$key // empty"
    else
        terraform -chdir="$tf_dir" output -json "$name" 2>/dev/null | jq -r '. // empty'
    fi
}

# Get Nebius CLI path
get_nebius_path() {
    if command -v nebius &>/dev/null; then
        command -v nebius
    elif [[ -x "$HOME/.nebius/bin/nebius" ]]; then
        echo "$HOME/.nebius/bin/nebius"
    fi
}

# Read secret from Nebius MysteryBox
# Usage: get_mysterybox_secret <secret_id> <key>
# Returns the secret value or empty string if not found
get_mysterybox_secret() {
    local secret_id=$1
    local key=$2
    local nebius_path=$(get_nebius_path)
    
    if [[ -z "$nebius_path" ]]; then
        log_warning "Nebius CLI not found, cannot read from MysteryBox"
        return 1
    fi
    
    if [[ -z "$secret_id" ]]; then
        return 1
    fi
    
    local result=$("$nebius_path" mysterybox v1 payload get-by-key \
        --secret-id "$secret_id" \
        --key "$key" \
        --format json 2>/dev/null)
    
    if [[ -n "$result" ]]; then
        echo "$result" | jq -r '.data.string_value // empty' 2>/dev/null
    fi
}

normalize_nebius_storage_endpoint() {
    local endpoint="$1"

    endpoint="${endpoint%/}"
    if [[ -z "$endpoint" ]]; then
        return 1
    fi

    if [[ "$endpoint" =~ ^https://[^/:]+$ ]]; then
        printf '%s:443\n' "$endpoint"
    else
        printf '%s\n' "$endpoint"
    fi
}

get_kubernetes_secret_value() {
    local ns="${1:-default}"
    local secret_name="$2"
    local key="$3"
    local encoded

    encoded=$(kubectl get secret "$secret_name" -n "$ns" -o jsonpath="{.data.${key}}" 2>/dev/null || echo "")
    if [[ -n "$encoded" ]]; then
        printf '%s' "$encoded" | base64 -d 2>/dev/null
    fi
}

sync_osmo_storage_secret() {
    local ns="${1:-osmo}"
    local tf_dir="${2:-../001-iac}"
    local access_key
    local secret_ref_id
    local secret_key

    access_key=$(get_tf_output "storage_credentials.access_key_id" "$tf_dir" 2>/dev/null || echo "")
    secret_ref_id=$(get_tf_output "storage_secret_reference_id" "$tf_dir" 2>/dev/null || echo "")
    secret_key=""

    if [[ -n "$secret_ref_id" ]]; then
        secret_key=$(get_mysterybox_secret "$secret_ref_id" "secret" 2>/dev/null || echo "")
    fi

    if [[ -z "$access_key" || -z "$secret_key" ]]; then
        log_error "Could not retrieve storage credentials from Terraform/MysteryBox"
        return 1
    fi

    kubectl create secret generic osmo-storage \
        --namespace "$ns" \
        --from-literal=access-key-id="${access_key}" \
        --from-literal=secret-access-key="${secret_key}" \
        --dry-run=client -o yaml | kubectl apply -f - >/dev/null

    log_success "osmo-storage secret synced"
}

probe_nebius_bucket_rw() {
    local ns="${1:-osmo}"
    local bucket="$2"
    local endpoint="$3"
    local region="$4"
    local secret_name="${5:-osmo-storage}"
    local access_key
    local secret_key
    local probe_key
    local tmp_file
    local run_name
    local probe_script

    if [[ -z "$bucket" || -z "$endpoint" || -z "$region" ]]; then
        log_error "Bucket probe requires bucket, endpoint, and region"
        return 1
    fi

    endpoint=$(normalize_nebius_storage_endpoint "$endpoint") || return 1
    access_key=$(get_kubernetes_secret_value "$ns" "$secret_name" "access-key-id")
    secret_key=$(get_kubernetes_secret_value "$ns" "$secret_name" "secret-access-key")

    if [[ -z "$access_key" || -z "$secret_key" ]]; then
        log_error "Secret ${secret_name} in namespace ${ns} is missing storage credentials"
        return 1
    fi

    probe_key="osmo-storage-probe-$(date +%s)-${RANDOM}"

    if check_command aws; then
        tmp_file=$(mktemp "/tmp/osmo-storage-probe.XXXXXX")
        printf 'osmo-storage-probe\n' >"$tmp_file"

        if AWS_EC2_METADATA_DISABLED=true \
            AWS_ACCESS_KEY_ID="$access_key" \
            AWS_SECRET_ACCESS_KEY="$secret_key" \
            AWS_DEFAULT_REGION="$region" \
            aws --endpoint-url "$endpoint" s3api head-bucket --bucket "$bucket" >/dev/null 2>&1 &&
            AWS_EC2_METADATA_DISABLED=true \
            AWS_ACCESS_KEY_ID="$access_key" \
            AWS_SECRET_ACCESS_KEY="$secret_key" \
            AWS_DEFAULT_REGION="$region" \
            aws --endpoint-url "$endpoint" s3api put-object --bucket "$bucket" --key "$probe_key" --body "$tmp_file" >/dev/null 2>&1 &&
            AWS_EC2_METADATA_DISABLED=true \
            AWS_ACCESS_KEY_ID="$access_key" \
            AWS_SECRET_ACCESS_KEY="$secret_key" \
            AWS_DEFAULT_REGION="$region" \
            aws --endpoint-url "$endpoint" s3api delete-object --bucket "$bucket" --key "$probe_key" >/dev/null 2>&1; then
            rm -f "$tmp_file" 2>/dev/null || true
            return 0
        fi

        rm -f "$tmp_file" 2>/dev/null || true
    fi

    run_name="osmo-s3-probe-${RANDOM}${RANDOM}"
    probe_script=$(cat <<EOF
set -e
aws --endpoint-url "$endpoint" s3api head-bucket --bucket "$bucket" >/dev/null
aws --endpoint-url "$endpoint" s3api put-object --bucket "$bucket" --key "$probe_key" --body /etc/hosts >/dev/null
aws --endpoint-url "$endpoint" s3api delete-object --bucket "$bucket" --key "$probe_key" >/dev/null
echo S3_PROBE_OK
EOF
)

    if kubectl run "$run_name" \
        --rm --restart=Never -i \
        -n "$ns" \
        --image=amazon/aws-cli:2.15.0 \
        --env="AWS_ACCESS_KEY_ID=${access_key}" \
        --env="AWS_SECRET_ACCESS_KEY=${secret_key}" \
        --env="AWS_DEFAULT_REGION=${region}" \
        --env="AWS_EC2_METADATA_DISABLED=true" \
        --command -- \
        sh -lc "$probe_script" >/tmp/_osmo_s3_probe.out 2>/tmp/_osmo_s3_probe.err; then
        if grep -q "S3_PROBE_OK" /tmp/_osmo_s3_probe.out 2>/dev/null; then
            rm -f /tmp/_osmo_s3_probe.out /tmp/_osmo_s3_probe.err 2>/dev/null || true
            return 0
        fi
    fi

    cat /tmp/_osmo_s3_probe.out 2>/dev/null >&2 || true
    cat /tmp/_osmo_s3_probe.err 2>/dev/null >&2 || true
    rm -f /tmp/_osmo_s3_probe.out /tmp/_osmo_s3_probe.err 2>/dev/null || true
    return 1
}

# Read the Postgres connection settings from the live osmo-service deployment.
discover_osmo_service_postgres_config() {
    local ns="${1:-osmo}"
    local deployment_json
    local pg_host
    local pg_port
    local pg_db
    local pg_user
    local pg_password

    deployment_json=$(kubectl get deployment osmo-service -n "$ns" -o json 2>/dev/null) || {
        log_error "Could not read deployment/osmo-service in namespace ${ns}"
        return 1
    }

    pg_host=$(echo "$deployment_json" | jq -r '[.spec.template.spec.containers[] | select(.name=="osmo-service") | .env[]? | select(.name=="OSMO_POSTGRES_HOST") | .value][0] // empty')
    pg_port=$(echo "$deployment_json" | jq -r '[.spec.template.spec.containers[] | select(.name=="osmo-service") | .env[]? | select(.name=="OSMO_POSTGRES_PORT") | .value][0] // empty')
    pg_db=$(echo "$deployment_json" | jq -r '[.spec.template.spec.containers[] | select(.name=="osmo-service") | .env[]? | select(.name=="OSMO_POSTGRES_DATABASE") | .value][0] // empty')
    pg_user=$(echo "$deployment_json" | jq -r '[.spec.template.spec.containers[] | select(.name=="osmo-service") | .env[]? | select(.name=="OSMO_POSTGRES_USER") | .value][0] // empty')
    pg_password=$(kubectl get secret db-secret -n "$ns" -o jsonpath='{.data.db-password}' 2>/dev/null | base64 -d)

    if [[ -z "$pg_host" || -z "$pg_port" || -z "$pg_db" || -z "$pg_user" || -z "$pg_password" ]]; then
        log_error "Could not discover OSMO Postgres settings from the live cluster"
        return 1
    fi

    printf '%s\t%s\t%s\t%s\t%s\n' "$pg_host" "$pg_port" "$pg_db" "$pg_user" "$pg_password"
}

sanitize_kubectl_run_output() {
    awk '
        /^All commands and output from this session/ { next }
        /^If you don/ { next }
        /^warning: couldn.t attach to pod/ { next }
        /^pod ".*" deleted from .* namespace$/ { next }
        /^pod .* terminated \(Completed\)$/ { next }
        /^pod .* terminated \(Error\)$/ { next }
        { print }
    '
}

# Run a SQL statement against the live OSMO Postgres database via an ephemeral psql pod.
run_osmo_postgres_sql() {
    local sql="$1"
    local ns="${2:-osmo}"
    local pg_host="${3:-${POSTGRES_HOST:-}}"
    local pg_port="${4:-${POSTGRES_PORT:-}}"
    local pg_db="${5:-${POSTGRES_DB:-${OSMO_POSTGRES_DATABASE:-osmo}}}"
    local pg_user="${6:-${POSTGRES_USER:-}}"
    local pg_password="${7:-${POSTGRES_PASSWORD:-}}"
    local discovered=""
    local attempt
    local run_name
    local output
    local cleaned

    if [[ -z "$pg_host" || -z "$pg_port" || -z "$pg_db" || -z "$pg_user" || -z "$pg_password" ]]; then
        discovered=$(discover_osmo_service_postgres_config "$ns") || return 1
        IFS=$'\t' read -r pg_host pg_port pg_db pg_user pg_password <<<"$discovered"
    fi

    for attempt in 1 2 3; do
        run_name="osmo-psql-${RANDOM}${RANDOM}"
        log_info "Attempt ${attempt}/3: running PostgreSQL config query..."

        if output=$(kubectl run "$run_name" \
            --rm --restart=Never -i \
            -n "$ns" \
            --image=postgres:16-alpine \
            --env="PGPASSWORD=${pg_password}" \
            --command -- \
            psql \
                -h "$pg_host" \
                -p "$pg_port" \
                -U "$pg_user" \
                -d "$pg_db" \
                -v ON_ERROR_STOP=1 \
                -qAtc "$sql" 2>&1); then
            cleaned=$(printf '%s\n' "$output" | sanitize_kubectl_run_output | sed '/^$/d')
            [[ -n "$cleaned" ]] && printf '%s\n' "$cleaned"
            return 0
        fi

        cleaned=$(printf '%s\n' "$output" | sanitize_kubectl_run_output | sed '/^$/d')
        [[ -n "$cleaned" ]] && printf '%s\n' "$cleaned" >&2

        if [[ "$attempt" -lt 3 ]]; then
            log_warning "PostgreSQL query failed, retrying in $((attempt * 2))s..."
            sleep $((attempt * 2))
        fi
    done

    log_error "Failed to run PostgreSQL query after 3 attempts"
    return 1
}

sql_escape_literal() {
    printf '%s' "$1" | sed "s/'/''/g"
}

get_osmo_config_value_db() {
    local ns="${1:-osmo}"
    local config_type="$2"
    local key="$3"
    local pg_host="${4:-${POSTGRES_HOST:-}}"
    local pg_port="${5:-${POSTGRES_PORT:-}}"
    local pg_db="${6:-${POSTGRES_DB:-${OSMO_POSTGRES_DATABASE:-osmo}}}"
    local pg_user="${7:-${POSTGRES_USER:-}}"
    local pg_password="${8:-${POSTGRES_PASSWORD:-}}"
    local escaped_type
    local escaped_key

    escaped_type=$(sql_escape_literal "$config_type")
    escaped_key=$(sql_escape_literal "$key")
    run_osmo_postgres_sql \
        "select value from configs where type = '${escaped_type}' and key = '${escaped_key}';" \
        "$ns" "$pg_host" "$pg_port" "$pg_db" "$pg_user" "$pg_password"
}

osmo_config_key_exists_db() {
    local ns="${1:-osmo}"
    local config_type="$2"
    local key="$3"
    local pg_host="${4:-${POSTGRES_HOST:-}}"
    local pg_port="${5:-${POSTGRES_PORT:-}}"
    local pg_db="${6:-${POSTGRES_DB:-${OSMO_POSTGRES_DATABASE:-osmo}}}"
    local pg_user="${7:-${POSTGRES_USER:-}}"
    local pg_password="${8:-${POSTGRES_PASSWORD:-}}"
    local value

    value=$(get_osmo_config_value_db "$ns" "$config_type" "$key" "$pg_host" "$pg_port" "$pg_db" "$pg_user" "$pg_password" 2>/dev/null || true)
    [[ -n "$value" ]]
}

upsert_osmo_config_value_db() {
    local ns="${1:-osmo}"
    local config_type="$2"
    local key="$3"
    local value="$4"
    local pg_host="${5:-${POSTGRES_HOST:-}}"
    local pg_port="${6:-${POSTGRES_PORT:-}}"
    local pg_db="${7:-${POSTGRES_DB:-${OSMO_POSTGRES_DATABASE:-osmo}}}"
    local pg_user="${8:-${POSTGRES_USER:-}}"
    local pg_password="${9:-${POSTGRES_PASSWORD:-}}"
    local escaped_type
    local escaped_key
    local escaped_value

    if [[ -z "$config_type" || -z "$key" ]]; then
        log_error "config_type and key must not be empty"
        return 1
    fi

    escaped_type=$(sql_escape_literal "$config_type")
    escaped_key=$(sql_escape_literal "$key")
    escaped_value=$(sql_escape_literal "$value")
    run_osmo_postgres_sql \
        "with updated as (
            update configs
               set value = '${escaped_value}'
             where type = '${escaped_type}'
               and key = '${escaped_key}'
         returning 1
        )
        insert into configs(key, value, type)
        select '${escaped_key}', '${escaped_value}', '${escaped_type}'
         where not exists (select 1 from updated);" \
        "$ns" "$pg_host" "$pg_port" "$pg_db" "$pg_user" "$pg_password" >/dev/null
}

delete_osmo_config_value_db() {
    local ns="${1:-osmo}"
    local config_type="$2"
    local key="$3"
    local pg_host="${4:-${POSTGRES_HOST:-}}"
    local pg_port="${5:-${POSTGRES_PORT:-}}"
    local pg_db="${6:-${POSTGRES_DB:-${OSMO_POSTGRES_DATABASE:-osmo}}}"
    local pg_user="${7:-${POSTGRES_USER:-}}"
    local pg_password="${8:-${POSTGRES_PASSWORD:-}}"
    local escaped_type
    local escaped_key

    escaped_type=$(sql_escape_literal "$config_type")
    escaped_key=$(sql_escape_literal "$key")
    run_osmo_postgres_sql \
        "delete from configs where type = '${escaped_type}' and key = '${escaped_key}';" \
        "$ns" "$pg_host" "$pg_port" "$pg_db" "$pg_user" "$pg_password" >/dev/null
}

get_osmo_service_config_value_db() {
    local ns="${1:-osmo}"
    local key="$2"
    local pg_host="${3:-${POSTGRES_HOST:-}}"
    local pg_port="${4:-${POSTGRES_PORT:-}}"
    local pg_db="${5:-${POSTGRES_DB:-${OSMO_POSTGRES_DATABASE:-osmo}}}"
    local pg_user="${6:-${POSTGRES_USER:-}}"
    local pg_password="${7:-${POSTGRES_PASSWORD:-}}"

    get_osmo_config_value_db "$ns" SERVICE "$key" "$pg_host" "$pg_port" "$pg_db" "$pg_user" "$pg_password"
}

osmo_service_config_key_exists() {
    local ns="${1:-osmo}"
    local key="$2"
    local pg_host="${3:-${POSTGRES_HOST:-}}"
    local pg_port="${4:-${POSTGRES_PORT:-}}"
    local pg_db="${5:-${POSTGRES_DB:-${OSMO_POSTGRES_DATABASE:-osmo}}}"
    local pg_user="${6:-${POSTGRES_USER:-}}"
    local pg_password="${7:-${POSTGRES_PASSWORD:-}}"

    osmo_config_key_exists_db "$ns" SERVICE "$key" "$pg_host" "$pg_port" "$pg_db" "$pg_user" "$pg_password"
}

# Upsert SERVICE.service_base_url directly in Postgres.
# This avoids the public SERVICE PATCH path, which can corrupt service_auth on reruns.
upsert_osmo_service_base_url_db() {
    local ns="${1:-osmo}"
    local service_url="$2"
    local pg_host="${3:-${POSTGRES_HOST:-}}"
    local pg_port="${4:-${POSTGRES_PORT:-}}"
    local pg_db="${5:-${POSTGRES_DB:-${OSMO_POSTGRES_DATABASE:-osmo}}}"
    local pg_user="${6:-${POSTGRES_USER:-}}"
    local pg_password="${7:-${POSTGRES_PASSWORD:-}}"

    if [[ -z "$service_url" ]]; then
        log_error "service_url must not be empty"
        return 1
    fi

    upsert_osmo_config_value_db "$ns" SERVICE service_base_url "$service_url" \
        "$pg_host" "$pg_port" "$pg_db" "$pg_user" "$pg_password"
}

# Delete SERVICE.service_auth so osmo-service regenerates it on restart.
delete_osmo_service_auth_db() {
    local ns="${1:-osmo}"
    local pg_host="${2:-${POSTGRES_HOST:-}}"
    local pg_port="${3:-${POSTGRES_PORT:-}}"
    local pg_db="${4:-${POSTGRES_DB:-${OSMO_POSTGRES_DATABASE:-osmo}}}"
    local pg_user="${5:-${POSTGRES_USER:-}}"
    local pg_password="${6:-${POSTGRES_PASSWORD:-}}"

    delete_osmo_config_value_db "$ns" SERVICE service_auth \
        "$pg_host" "$pg_port" "$pg_db" "$pg_user" "$pg_password"
}

# -----------------------------------------------------------------------------
# OSMO API helpers (for use when Envoy auth sidecar is present)
# -----------------------------------------------------------------------------
# Per OSMO documentation, the OSMO service authorises requests by reading
# the x-osmo-user and x-osmo-roles headers.  Envoy normally sets these from
# the JWT but when we bypass Envoy (port-forward to pod:8000) we must set
# them ourselves.
#
# Reference: https://nvidia.github.io/OSMO/main/deployment_guide/appendix/authentication/authentication_flow.html

# Detect if a pod has an Envoy sidecar container
# Usage: has_envoy_sidecar <namespace> <label-selector>
# Returns 0 (true) if envoy container is found, 1 (false) otherwise
has_envoy_sidecar() {
    local ns="$1"
    local label="$2"
    local pod_name
    pod_name=$(kubectl get pod -n "$ns" -l "$label" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
    if [[ -z "$pod_name" ]]; then
        return 1
    fi
    kubectl get pod -n "$ns" "$pod_name" -o jsonpath='{.spec.containers[*].name}' 2>/dev/null | grep -q envoy
}

# Start a port-forward that bypasses Envoy when the sidecar is present.
# Sets PORT_FORWARD_PID and prints log messages.
# Usage: start_osmo_port_forward <namespace> [local_port]
start_osmo_port_forward() {
    local ns="${1:-osmo}"
    local local_port="${2:-8080}"

    if has_envoy_sidecar "$ns" "app=osmo-service"; then
        local pod_name
        # Prefer a Running pod so we don't port-forward to one stuck in ContainerCreating
        pod_name=$(kubectl get pod -n "$ns" -l app=osmo-service --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
        [[ -z "$pod_name" ]] && pod_name=$(kubectl get pod -n "$ns" -l app=osmo-service -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
        log_info "Envoy sidecar detected -- port-forwarding to pod/${pod_name}:8000 (bypassing auth)..."
        start_kubectl_port_forward "$ns" "pod/${pod_name}" 8000 "$local_port" "OSMO API pod/${pod_name}:8000" || return 1
        _OSMO_AUTH_BYPASS=true
    else
        log_info "No Envoy sidecar -- port-forwarding to svc/osmo-service:80..."
        start_kubectl_port_forward "$ns" svc/osmo-service 80 "$local_port" "OSMO API svc/osmo-service:80" || return 1
        _OSMO_AUTH_BYPASS=false
    fi
    export _OSMO_AUTH_BYPASS
}

# Wait for the forwarded OSMO API to respond.
wait_for_osmo_api() {
    local port="${1:-${PORT_FORWARD_PORT:-8080}}"
    local timeout="${2:-30}"
    wait_for_http_ready "http://localhost:${port}/api/version" "$timeout" "OSMO API"
}

# Start a port-forward and wait until the OSMO API responds.
# Sets OSMO_API_PORT and OSMO_API_URL.
start_osmo_api_session() {
    local ns="${1:-osmo}"
    local preferred_local_port="${2:-8080}"
    local timeout="${3:-30}"

    start_osmo_port_forward "$ns" "$preferred_local_port" || return 1
    wait_for_osmo_api "${PORT_FORWARD_PORT}" "$timeout" || {
        stop_port_forward
        return 1
    }

    OSMO_API_PORT="${PORT_FORWARD_PORT}"
    OSMO_API_URL="http://localhost:${OSMO_API_PORT}"
    export OSMO_API_PORT OSMO_API_URL
}

# Make an authenticated curl call to the OSMO API.
# When _OSMO_AUTH_BYPASS=true (Envoy bypassed), injects x-osmo-user and
# x-osmo-roles headers so the OSMO service authorises the request.
# Usage: osmo_curl <method> <url> [curl-args...]
# Example: osmo_curl GET "http://localhost:8080/api/configs/service"
# Example: osmo_curl PATCH "http://localhost:8080/api/configs/service" -d '{"configs_dict":{...}}'
osmo_curl() {
    local method="$1"; shift
    local url="$1"; shift

    local auth_args=()
    if [[ "${_OSMO_AUTH_BYPASS:-false}" == "true" ]]; then
        auth_args+=(-H "x-osmo-user: osmo-admin" -H "x-osmo-roles: osmo-admin,osmo-user")
    fi

    curl -sS \
        --connect-timeout "${OSMO_CURL_CONNECT_TIMEOUT:-5}" \
        --max-time "${OSMO_CURL_MAX_TIME:-120}" \
        -X "$method" "$url" \
        -H "Content-Type: application/json" \
        "${auth_args[@]}" \
        "$@"
}

# Log in to OSMO using the appropriate method.
# When bypassing Envoy this is a no-op (curl headers handle auth).
# Otherwise uses `osmo login --method dev`.
# Usage: osmo_login [port]
osmo_login() {
    local port="${1:-8080}"
    if [[ "${_OSMO_AUTH_BYPASS:-false}" == "true" ]]; then
        log_info "Auth bypass active -- using direct API headers (osmo-admin role)"
    else
        log_info "Logging in to OSMO..."
        if ! osmo login "http://localhost:${port}" --method dev --username admin 2>/dev/null; then
            log_error "Failed to login to OSMO"
            return 1
        fi
        log_success "Logged in successfully"
    fi
}

# Update an OSMO config via the PATCH API (partial merge).
# When _OSMO_AUTH_BYPASS=true, uses curl; otherwise uses osmo CLI.
# Usage: osmo_config_update <CONFIG_TYPE> <json_file> <description>
# Example: osmo_config_update WORKFLOW /tmp/config.json "Configure storage"
osmo_config_update() {
    local config_type="$1"
    local json_file="$2"
    local description="${3:-Update config}"
    local port="${4:-8080}"

    if [[ "${_OSMO_AUTH_BYPASS:-false}" == "true" ]]; then
        local endpoint
        endpoint="api/configs/$(echo "$config_type" | tr '[:upper:]' '[:lower:]')"

        # Build PATCH request body: {"description": "...", "configs_dict": <file-contents>}
        local body
        body=$(jq -n --arg desc "$description" --slurpfile cfg "$json_file" \
            '{description: $desc, configs_dict: $cfg[0]}')

        local http_code
        http_code=$(osmo_curl PATCH "http://localhost:${port}/${endpoint}" \
            -d "$body" -o /tmp/_osmo_patch_resp.txt -w "%{http_code}")

        if [[ "$http_code" =~ ^2 ]]; then
            return 0
        else
            log_error "PATCH /${endpoint} returned HTTP ${http_code}"
            cat /tmp/_osmo_patch_resp.txt 2>/dev/null || true
            return 1
        fi
    else
        osmo config update "$config_type" --file "$json_file" --description "$description" 2>/dev/null
    fi
}
