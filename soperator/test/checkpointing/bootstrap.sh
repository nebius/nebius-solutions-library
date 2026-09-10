#!/bin/bash
# Prepares the shared environment for the checkpointing examples.
# Run once from a login node; the venv lands in the jail, so all nodes see it.
set -euo pipefail

CHECKPOINTING_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${CHECKPOINTING_DIR}/venv"
ENV_FILE='/etc/nebius-checkpoints.env'
BIN_DIR="${CHECKPOINTING_DIR}/bin"

require_command() {
  if ! command -v "$1" >/dev/null; then
    echo "ERROR: required command '$1' is not installed. $2" >&2
    exit 1
  fi
}

require_command python3 "Install Python 3 with the venv module on the login node."
require_command curl "Install curl on the login node."
require_command tar "Install tar on the login node."
require_command sha256sum "Install GNU coreutils on the login node."
if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
  echo "ERROR: Python 3.10 or newer is required on the login node." >&2
  exit 1
fi

# Slurm opens output files before the batch script starts, so create the shared
# results directory during bootstrap rather than requiring a manual extra step.
mkdir -p "${CHECKPOINTING_DIR}/results" "${BIN_DIR}"

if [ ! -f "${ENV_FILE}" ]; then
  echo "ERROR: ${ENV_FILE} not found." >&2
  echo "It is created from the jail-checkpoints k8s secret (checkpoints_access module)." >&2
  echo "Make sure checkpoint_storage_enabled = true in your Terraform installation." >&2
  exit 1
fi

if [ ! -d "${VENV_DIR}" ]; then
  echo "Creating venv at ${VENV_DIR}..."
  if ! python3 -m venv "${VENV_DIR}"; then
    echo "ERROR: Python could not create a virtual environment. Install the distribution's python3-venv package." >&2
    exit 1
  fi
fi

echo "Installing pinned dependencies (torch download is large, be patient)..."
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet --requirement "${CHECKPOINTING_DIR}/requirements.txt"

S5CMD="${BIN_DIR}/s5cmd"
if [ -x "${S5CMD}" ]; then
  case "$("${S5CMD}" version 2>/dev/null || true)" in
    v2.3.0-*) ;;
    *)
      echo "Replacing unexpected s5cmd version in ${S5CMD}..."
      rm -f "${S5CMD}"
      ;;
  esac
fi
if [ ! -x "${S5CMD}" ]; then
  echo "Installing pinned s5cmd into the shared checkpointing directory..."
  case "$(uname -m)" in
    x86_64 | amd64)
      S5CMD_ARCH="64bit"
      S5CMD_SHA256="de0fdbfa3aceae55e069ba81a0fc17b2026567637603734a387b2fca06c299b4"
      ;;
    aarch64 | arm64)
      S5CMD_ARCH="arm64"
      S5CMD_SHA256="1439f0d00ecedcd2a2f1f2c6749bbb0152b2257bf5086f29646ec8ae38798e24"
      ;;
    *)
      echo "ERROR: s5cmd has no configured binary for architecture $(uname -m)" >&2
      exit 1
      ;;
  esac
  archive="$(mktemp)"
  trap 'rm -f "${archive:-}"' EXIT
  curl --fail --silent --show-error --location --retry 3 \
    --output "${archive}" \
    "https://github.com/peak/s5cmd/releases/download/v2.3.0/s5cmd_2.3.0_Linux-${S5CMD_ARCH}.tar.gz"
  printf '%s  %s\n' "${S5CMD_SHA256}" "${archive}" | sha256sum --check --status || {
    echo "ERROR: downloaded s5cmd archive failed its SHA-256 check" >&2
    exit 1
  }
  tar xzf "${archive}" -C "${BIN_DIR}" s5cmd
  chmod 755 "${S5CMD}"
  rm -f "${archive}"
  trap - EXIT
fi
export PATH="${BIN_DIR}:${PATH}"

echo "Verifying Nebius Object Storage access with the checkpoint credentials..."
set -a
# shellcheck disable=SC1090 # generated installation-specific environment file
source "${ENV_FILE}"
set +a
"${VENV_DIR}/bin/python" - <<'EOF'
import os
import socket
import uuid

import boto3
from botocore.config import Config

endpoint = os.environ["NEBIUS_OBJECT_STORAGE_ENDPOINT"]
bucket = os.environ["NEBIUS_CHECKPOINT_BUCKET"]
region = os.environ["NEBIUS_OBJECT_STORAGE_REGION"]
object_storage = boto3.client(
    "s3",
    endpoint_url=endpoint,
    region_name=region,
    config=Config(
        s3={"addressing_style": "path"},
        retries={"max_attempts": 5, "mode": "standard"},
    ),
)
key = f".soperator/bootstrap-check/{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex}"
created = False
try:
    object_storage.put_object(Bucket=bucket, Key=key, Body=b"ok")
    created = True
    if object_storage.get_object(Bucket=bucket, Key=key)["Body"].read() != b"ok":
        raise RuntimeError("Object Storage bootstrap probe returned unexpected content")
finally:
    if created:
        object_storage.delete_object(Bucket=bucket, Key=key)
print(f"Nebius Object Storage access OK: bucket={bucket} endpoint={endpoint}")
EOF

"${VENV_DIR}/bin/python" -c 'import torch, s3torchconnector.dcp; print("torch", torch.__version__, "/ s3torchconnector DCP import OK")'
echo "Bootstrap done."
