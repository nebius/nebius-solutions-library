#!/usr/bin/env bash
set -euo pipefail

: "${K8S_CLUSTER_CONTEXT:?K8S_CLUSTER_CONTEXT is required}"
: "${SLURM_CLUSTER_NAME:?SLURM_CLUSTER_NAME is required}"
: "${SLURM_CLUSTER_NAMESPACE:?SLURM_CLUSTER_NAMESPACE is required}"

readonly timeout_seconds=3600
readonly retry_interval_seconds=5

start_time=$(date +%s)
readonly start_time
readonly deadline=$((start_time + timeout_seconds))

attempt=1
while true; do
    current_time=$(date +%s)
    remaining_seconds=$((deadline - current_time))
    if ((remaining_seconds <= 0)); then
        echo "Timeout waiting for SlurmCluster ${SLURM_CLUSTER_NAMESPACE}/${SLURM_CLUSTER_NAME} to become Available." >&2
        exit 1
    fi

    echo "Waiting for SlurmCluster ${SLURM_CLUSTER_NAMESPACE}/${SLURM_CLUSTER_NAME} to become Available (attempt ${attempt}, ${remaining_seconds}s remaining)..."
    if kubectl wait \
        --for=jsonpath='{.status.phase}'=Available \
        --timeout="${remaining_seconds}s" \
        --context "$K8S_CLUSTER_CONTEXT" \
        -n "$SLURM_CLUSTER_NAMESPACE" \
        "slurmcluster.slurm.nebius.ai/${SLURM_CLUSTER_NAME}"; then
        exit 0
    fi

    current_time=$(date +%s)
    remaining_seconds=$((deadline - current_time))
    if ((remaining_seconds <= 0)); then
        echo "Timeout waiting for SlurmCluster ${SLURM_CLUSTER_NAMESPACE}/${SLURM_CLUSTER_NAME} to become Available." >&2
        exit 1
    fi

    sleep_seconds=$retry_interval_seconds
    if ((sleep_seconds > remaining_seconds)); then
        sleep_seconds=$remaining_seconds
    fi

    echo "kubectl wait failed before the overall timeout; retrying in ${sleep_seconds}s..." >&2
    sleep "$sleep_seconds"
    attempt=$((attempt + 1))
done
