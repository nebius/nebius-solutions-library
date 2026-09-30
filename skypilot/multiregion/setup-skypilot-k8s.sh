#!/usr/bin/env bash

# setup-skypilot-k8s.sh
#
# Purpose:  Register Kubernetes cluster credentials produced by Terraform
#           with the local kubectl config and add the discovered contexts to
#           the SkyPilot config (`~/.sky/config.yaml`) so SkyPilot can target
#           Kubernetes clusters.
#
# Usage:    Run this script from the repository (or set `TF_DIR` to the
#           directory containing the Terraform state/outputs):
#
#   TF_DIR=./skypilot/multiregion ./skypilot/multiregion/setup-skypilot-k8s.sh
#
# Prereqs:  terraform, jq, nebius, kubectl, sky, yq
#
# Environment variables:
#   TF_DIR:          Directory where Terraform outputs are (default: script dir)
#   SKY_CONFIG_PATH: Location of SkyPilot config (default: $HOME/.sky/config.yaml)
#
# Effect:   Reads the Terraform output `kube_cluster`, obtains kube credentials
#           with `nebius`, finds the corresponding kubectl contexts, and appends
#           them to `kubernetes.allowed_contexts` in the SkyPilot config.
#
# Examples:
#   sky launch -c k8s-cpu --cloud kubernetes "echo hello from skypilot on k8s"
#   sky launch -c k8s-gpu --cloud kubernetes --gpus H200 "nvidia-smi"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="${TF_DIR:-$SCRIPT_DIR}"
SKY_CONFIG_PATH="${SKY_CONFIG_PATH:-$HOME/.sky/config.yaml}"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Error: required command not found: $1" >&2
    exit 1
  fi
}

require_sky_cmd() {
  if command -v sky >/dev/null 2>&1; then
    return
  fi

  echo "Error: required command not found: sky" >&2
  if [[ -z "${VIRTUAL_ENV:-}" ]]; then
    if [[ -f "$SCRIPT_DIR/.venv/bin/activate" ]]; then
      echo "It looks like the Python virtual environment is not activated." >&2
      echo "Activate it and rerun:" >&2
      echo "  source \"$SCRIPT_DIR/.venv/bin/activate\"" >&2
    else
      echo "SkyPilot may not be installed in the active shell environment." >&2
      echo "Install it and rerun, e.g.:" >&2
      echo "  pip install \"skypilot-nightly[kubernetes]==1.0.0.dev20260219\"" >&2
    fi
  else
    echo "A virtual environment is active ($VIRTUAL_ENV), but 'sky' is not available." >&2
    echo "Install SkyPilot into this environment and rerun." >&2
  fi
  exit 1
}

require_cmd terraform
require_cmd jq
require_cmd nebius
require_cmd kubectl
require_sky_cmd
require_cmd yq

# Avoid stale env token overriding Nebius CLI auth and causing anonymous kube requests.
if [[ -n "${NEBIUS_IAM_TOKEN:-}" ]]; then
  echo "Warning: NEBIUS_IAM_TOKEN is set in the environment."
  echo "Unsetting it for this run to avoid invalid-token auth failures."
  unset NEBIUS_IAM_TOKEN
fi

if ! nebius iam get-access-token >/dev/null 2>&1; then
  echo "Error: failed to get Nebius IAM token from CLI auth." >&2
  echo "Run 'nebius profile auth' (or configure your profile), then rerun this script." >&2
  exit 1
fi

if ! terraform -chdir="$TF_DIR" output -json kube_cluster >/dev/null 2>&1; then
  echo "Error: failed to read terraform output 'kube_cluster' from $TF_DIR." >&2
  echo "Run terraform apply first." >&2
  exit 1
fi

KUBE_CLUSTERS_JSON="$(terraform -chdir="$TF_DIR" output -json kube_cluster)"

if ! jq -e 'type == "object" and length > 0' >/dev/null <<<"$KUBE_CLUSTERS_JSON"; then
  echo "Error: terraform output 'kube_cluster' is empty." >&2
  exit 1
fi

CLUSTERS=()
while IFS= read -r LINE; do
  CLUSTERS+=("$LINE")
done < <(jq -r 'to_entries[] | [.key, .value.id, .value.name] | @tsv' <<<"$KUBE_CLUSTERS_JSON")

declare -a CONTEXTS=()
for ENTRY in "${CLUSTERS[@]}"; do
  REGION_KEY="$(cut -f1 <<<"$ENTRY")"
  CLUSTER_ID="$(cut -f2 <<<"$ENTRY")"
  CLUSTER_NAME="$(cut -f3 <<<"$ENTRY")"

  echo "Registering kube credentials for $REGION_KEY ($CLUSTER_NAME)..."
  nebius mk8s v1 cluster get-credentials --id "$CLUSTER_ID" --external --force >/dev/null

  EXPECTED_CONTEXT="nebius-mk8s-$CLUSTER_NAME"
  if kubectl config get-contexts "$EXPECTED_CONTEXT" >/dev/null 2>&1; then
    CONTEXTS+=("$EXPECTED_CONTEXT")
    continue
  fi

  DETECTED_CONTEXT="$(kubectl config get-contexts -o name | grep -E "nebius-mk8s-${CLUSTER_NAME}$" | head -n1 || true)"
  if [[ -z "$DETECTED_CONTEXT" ]]; then
    echo "Error: unable to locate kubectl context for cluster $CLUSTER_NAME." >&2
    exit 1
  fi
  CONTEXTS+=("$DETECTED_CONTEXT")
done

mkdir -p "$(dirname "$SKY_CONFIG_PATH")"
if [[ ! -f "$SKY_CONFIG_PATH" ]]; then
  cat >"$SKY_CONFIG_PATH" <<'EOF'
kubernetes:
  allowed_contexts: []
EOF
fi

CONTEXTS_JSON="$(printf '%s\n' "${CONTEXTS[@]}" | jq -Rsc 'split("\n")[:-1]')"
yq e -i ".kubernetes.allowed_contexts = ((.kubernetes.allowed_contexts // []) + ${CONTEXTS_JSON} | unique)" "$SKY_CONFIG_PATH"

echo
echo "Configured SkyPilot kubernetes contexts:"
for CONTEXT in "${CONTEXTS[@]}"; do
  echo "  - $CONTEXT"
done
echo

# SkyPilot API server can keep stale environment variables across invocations.
# Restart it best-effort and run check with NEBIUS_IAM_TOKEN removed.
sky api stop >/dev/null 2>&1 || true
env -u NEBIUS_IAM_TOKEN sky check kubernetes
echo
echo "Ready. Example jobs:"
echo '  sky launch -c k8s-cpu --cloud kubernetes "echo hello from skypilot on k8s"'
echo '  sky launch -c k8s-gpu --cloud kubernetes --gpus H200 "nvidia-smi"'
echo '  sky logs k8s-gpu'
echo '  sky down k8s-cpu k8s-gpu -y'
