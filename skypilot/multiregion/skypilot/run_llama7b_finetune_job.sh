#!/usr/bin/env bash
set -euo pipefail

# Launches the llama7b fine-tune SkyPilot job using skypilot/llama7b_finetune.yaml.
#
# Required:
#   HF_TOKEN
#
# Optional:
#   JOB_CLUSTER_NAME (default: llama7b-ft)
#   SKY_CONTEXT      (if set, passed as --context)
#   MLFLOW_TRACKING_URI
#   MLFLOW_TRACKING_USERNAME
#   MLFLOW_TRACKING_PASSWORD
#
# Example:
#   HF_TOKEN=... ./run_llama7b_finetune_job.sh

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
YAML_PATH="${SCRIPT_DIR}/llama7b_finetune.yaml"
JOB_CLUSTER_NAME="${JOB_CLUSTER_NAME:-llama7b-ft}"

if ! command -v sky >/dev/null 2>&1; then
  echo "ERROR: 'sky' CLI not found. Install and configure SkyPilot first." >&2
  exit 1
fi

if [[ ! -f "${YAML_PATH}" ]]; then
  echo "ERROR: Job spec not found at ${YAML_PATH}" >&2
  exit 1
fi

if [[ -z "${HF_TOKEN:-}" ]]; then
  echo "ERROR: HF_TOKEN is required." >&2
  echo "Set it first: export HF_TOKEN=<your_hf_token>" >&2
  exit 1
fi

cmd=(
  sky launch
  -c "${JOB_CLUSTER_NAME}"
  "${YAML_PATH}"
  --env "HF_TOKEN=${HF_TOKEN}"
)

if [[ -n "${SKY_CONTEXT:-}" ]]; then
  cmd+=(--context "${SKY_CONTEXT}")
fi

if [[ -n "${MLFLOW_TRACKING_URI:-}" ]]; then
  cmd+=(--secret "MLFLOW_TRACKING_URI=${MLFLOW_TRACKING_URI}")
fi
if [[ -n "${MLFLOW_TRACKING_USERNAME:-}" ]]; then
  cmd+=(--secret "MLFLOW_TRACKING_USERNAME=${MLFLOW_TRACKING_USERNAME}")
fi
if [[ -n "${MLFLOW_TRACKING_PASSWORD:-}" ]]; then
  cmd+=(--secret "MLFLOW_TRACKING_PASSWORD=${MLFLOW_TRACKING_PASSWORD}")
fi

echo "Launching fine-tune job '${JOB_CLUSTER_NAME}' with spec: ${YAML_PATH}"
printf 'Command: '
printf '%q ' "${cmd[@]}"
printf '\n'

"${cmd[@]}"
