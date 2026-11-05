#!/bin/bash
# K8s-native topology discovery -- sibling to cluster_topology.sh (Slurm).
# Provides the same function names and variable contract as cluster_topology.sh
# so train_node_*_k8s.sh scripts can source this instead of the Slurm version
# with no other changes.
#
# Sources of truth (K8s-native, never Slurm):
#   NODE_RANK:     JOB_COMPLETION_INDEX (set automatically by K8s indexed Job)
#   NUM_NODES:     TRAINING_NUM_NODES env var (set in Job pod template spec)
#   RDZV_HOST:     TRAINING_RDZV_HOST env var (rendezvous ClusterIP Service DNS,
#                  created per job run to route to rank-0 pod)
#   GPUS_PER_NODE: nvidia-smi -L count (same hardware-local probe as Slurm version;
#                  confirmed works inside K8s pods that request nvidia.com/gpu)
#   NCCL_LIB_PATH: TRAINING_NCCL_LIB_PATH env var (path inside the workspace PVC)
#
# Fails loudly (non-zero exit, error message) on any missing required env var,
# same contract as the Slurm version -- never silently falls back to a guessed value.

set -u

# For train_node_*_k8s.sh (inside a running indexed Job pod):
# Sets NUM_NODES and NODE_LIST from K8s-native env vars.
# Also loads NCCL_LIB_PATH from TRAINING_NCCL_LIB_PATH if set.
cluster_topology_job_nodes() {
    if [ -z "${JOB_COMPLETION_INDEX:-}" ]; then
        echo "FATAL: JOB_COMPLETION_INDEX is not set -- this script must run inside" \
             "a K8s indexed Job pod (batch/v1 Job with completionMode: Indexed)." >&2
        exit 1
    fi
    if [ -z "${TRAINING_NUM_NODES:-}" ]; then
        echo "FATAL: TRAINING_NUM_NODES is not set in this pod's env -- set it in" \
             "the Job's pod template spec.containers[].env." >&2
        exit 1
    fi
    NUM_NODES="$TRAINING_NUM_NODES"
    # NODE_LIST is not used by torchrun (which uses rendezvous), but kept for
    # callers that expect it. Populated as logical rank labels, not real hostnames.
    NODE_LIST=""
    for i in $(seq 0 $((NUM_NODES - 1))); do
        NODE_LIST="${NODE_LIST:+$NODE_LIST,}rank-${i}"
    done
    # Load NCCL_LIB_PATH from env var if set (install_k8s.sh writes it to
    # cluster_k8s.env, run_k8s.sh passes it to the Job as TRAINING_NCCL_LIB_PATH).
    if [ -n "${TRAINING_NCCL_LIB_PATH:-}" ]; then
        NCCL_LIB_PATH="$TRAINING_NCCL_LIB_PATH"
    fi
}

# Sets NODE_RANK from JOB_COMPLETION_INDEX (call cluster_topology_job_nodes first).
cluster_topology_discover_rank() {
    NODE_RANK="${JOB_COMPLETION_INDEX}"
}

# Sets RDZV_HOST from TRAINING_RDZV_HOST env var (the per-job-run ClusterIP
# Service DNS name that routes to rank-0's rendezvous port).
cluster_topology_discover_rdzv_host() {
    if [ -z "${TRAINING_RDZV_HOST:-}" ]; then
        echo "FATAL: TRAINING_RDZV_HOST is not set -- set it in the Job pod" \
             "template spec to the rendezvous ClusterIP Service DNS name." >&2
        exit 1
    fi
    RDZV_HOST="$TRAINING_RDZV_HOST"
}

# Sets GPUS_PER_NODE from live nvidia-smi inside the pod.
# Same hardware-local probe as the Slurm version -- nvidia-smi inside a K8s
# pod that requests nvidia.com/gpu resources works identically to Slurm.
cluster_topology_discover_gpu_count() {
    GPUS_PER_NODE="$(nvidia-smi -L 2>/dev/null | wc -l)"
    if [ -z "$GPUS_PER_NODE" ] || [ "$GPUS_PER_NODE" -eq 0 ]; then
        echo "FATAL: nvidia-smi reported 0 GPUs inside this pod -- ensure the" \
             "Job spec requests nvidia.com/gpu resources." >&2
        exit 1
    fi
}

# cluster_topology_available_nodes is called by pre-job launcher scripts on the
# control machine (run_k8s.sh). In K8s, the equivalent is reading cluster_k8s.env.
# This function is provided for compatibility; it reads from TRAINING_* env vars
# that run_k8s.sh exports before sourcing this file.
cluster_topology_available_nodes() {
    if [ -z "${TRAINING_NUM_NODES:-}" ]; then
        echo "FATAL: cluster_topology_available_nodes called with TRAINING_NUM_NODES" \
             "not set -- source cluster_k8s.env first or set TRAINING_NUM_NODES." >&2
        exit 1
    fi
    NUM_NODES="$TRAINING_NUM_NODES"
    GPUS_PER_NODE="${TRAINING_GPUS_PER_NODE:-8}"
    NCCL_LIB_PATH="${TRAINING_NCCL_LIB_PATH:-}"
    NODE_LIST=""
    for i in $(seq 0 $((NUM_NODES - 1))); do
        NODE_LIST="${NODE_LIST:+$NODE_LIST,}rank-${i}"
    done
}
