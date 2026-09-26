#!/bin/bash
# Stage 2 cluster-topology-agnostic fix -- real, live cluster discovery,
# sourced by every launch script in this package instead of each one
# separately hardcoding this project's original 2-node/8-GPU-per-node
# shape (STAGE2_HANDOFF.md items 1-3, 9). Two distinct discovery paths,
# because the two classes of caller run at genuinely different times:
#
#   - run_*.sh scripts build and submit the `srun` command itself, BEFORE
#     any job/allocation exists -- there is no SLURM_JOB_NODELIST yet.
#     These source cluster_topology_available_nodes(), which reads the
#     real, live node list/GPU count/NCCL path install.sh discovered once
#     (via `sinfo` + a live per-node nvidia-smi/NCCL probe) and persisted
#     to cluster.env alongside this package -- not re-queried per launch,
#     since which nodes are configured for this deployment doesn't change
#     between launches the way "which nodes are free right now" might.
#   - train_node_*.sh scripts (and the handful of workloads whose own
#     "run_*.sh" IS the per-node script, launched directly by srun with
#     no separate wrapper) run INSIDE the real srun allocation -- these
#     source cluster_topology_job_nodes(), which reads Slurm's own live,
#     real SLURM_JOB_NODELIST for THIS specific job, every time (never
#     install-time-cached, since it's genuinely different per job).
#
# Every function here fails loudly (real error message, non-zero exit)
# rather than silently falling back to a guessed value.

set -u

_CLUSTER_TOPOLOGY_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_CLUSTER_ENV="$_CLUSTER_TOPOLOGY_LIB_DIR/../cluster.env"

# For run_*.sh (pre-job): loads the real node list/GPU count/NCCL lib
# path install.sh discovered and persisted. Sets NODE_LIST (comma-
# separated, real hostnames, Slurm -w syntax), NUM_NODES, GPUS_PER_NODE
# (install.sh itself checks this is uniform across the fleet and warns if
# it isn't -- see its own output), and NCCL_LIB_PATH.
cluster_topology_available_nodes() {
  if [ ! -f "$_CLUSTER_ENV" ]; then
    echo "FATAL: $_CLUSTER_ENV not found -- run install.sh first. This" \
         "package no longer hardcodes a node list; install.sh's own live" \
         "cluster discovery is the only source of it." >&2
    exit 1
  fi
  # shellcheck disable=SC1090
  source "$_CLUSTER_ENV"
  if [ -z "${NODE_LIST:-}" ] || [ -z "${NUM_NODES:-}" ] || [ -z "${GPUS_PER_NODE:-}" ]; then
    echo "FATAL: cluster.env exists but is missing NODE_LIST/NUM_NODES/GPUS_PER_NODE -- re-run install.sh." >&2
    exit 1
  fi
}

# For train_node_*.sh / self-contained per-node scripts (inside the real
# allocation): sets NODE_LIST (comma-separated) and NUM_NODES from
# Slurm's own live job env -- genuinely real for THIS job, not
# install-time-cached. Also loads NCCL_LIB_PATH from cluster.env (that
# part IS install-time-fixed -- which NCCL build to link against is a
# deployment decision, not a per-job one).
cluster_topology_job_nodes() {
  if [ -z "${SLURM_JOB_NODELIST:-}" ]; then
    echo "FATAL: SLURM_JOB_NODELIST is not set -- this script must run" \
         "inside a real srun/sbatch allocation, not standalone." >&2
    exit 1
  fi
  if command -v scontrol >/dev/null 2>&1; then
    NODE_LIST="$(scontrol show hostnames "$SLURM_JOB_NODELIST" | paste -sd, -)"
  else
    # Real, live-confirmed gap: `scontrol` (a Slurm client binary) is
    # present on the bare host but NOT inside the training container
    # this code actually runs in (confirmed live: every workload launch
    # script execs this file via `bash -c '...'` inside the same
    # container srun starts training in). Fall back to the node list
    # install.sh already discovered on the host (from cluster.env) --
    # correct for this project's own real launch convention, where every
    # run_*.sh's own `-w "$NODE_LIST"` already requests the exact same
    # full cluster.env fleet, so "the job's real nodes" and "the fleet
    # cluster.env discovered" are the same real set in practice. If a
    # future job is launched against a genuine SUBSET of the fleet with
    # scontrol unavailable in-container, this will not reflect that --
    # a real, disclosed limitation of this fallback, not a silent one.
    if [ ! -f "$_CLUSTER_ENV" ]; then
      echo "FATAL: scontrol not found in this environment and" \
           "$_CLUSTER_ENV does not exist either -- no way to determine" \
           "the real node list. Run install.sh on the host first." >&2
      exit 1
    fi
    # shellcheck disable=SC1090
    source "$_CLUSTER_ENV"
  fi
  NUM_NODES="$(echo "$NODE_LIST" | tr ',' '\n' | wc -l)"
  # Load NCCL_LIB_PATH/VM_URL from cluster.env too (install-time-fixed
  # deployment decisions, not per-job ones) -- extracted directly rather
  # than a full `source`, so this never clobbers the NODE_LIST/NUM_NODES
  # scontrol may have already given us above with real, job-specific
  # values (cluster.env's own NODE_LIST is only the install-time fleet,
  # not necessarily this specific job's real allocation).
  if [ -f "$_CLUSTER_ENV" ]; then
    NCCL_LIB_PATH="$(grep -oP '^NCCL_LIB_PATH="\K[^"]*' "$_CLUSTER_ENV")"
    VM_URL="$(grep -oP '^VM_URL="\K[^"]*' "$_CLUSTER_ENV")"
  fi
}

# Sets NODE_RANK to this host's real, live 0-based position within
# NODE_LIST (call cluster_topology_job_nodes first). Generalizes the old
# `if hostname == "worker-0" then 0 else 1` two-way branch to any real
# node count -- fails loudly if this host isn't in the list at all,
# rather than silently defaulting to some rank.
cluster_topology_discover_rank() {
  local hn line_no
  hn="$(hostname)"
  line_no="$(echo "$NODE_LIST" | tr ',' '\n' | grep -n -x -F "$hn" | head -1 | cut -d: -f1)"
  if [ -z "$line_no" ]; then
    echo "FATAL: this host ($hn) was not found in the real Slurm node" \
         "list for this job ($NODE_LIST) -- cannot determine NODE_RANK." >&2
    exit 1
  fi
  NODE_RANK=$((line_no - 1))
}

# Sets RDZV_HOST to the real first entry of NODE_LIST, sorted -- this
# project's own established, disclosed launch convention (the same one
# alert_engine.py's _find_true_rank0_member already relies on): node_rank
# 0, and so the rendezvous host, is always the alphabetically-first node
# Slurm gave this job.
cluster_topology_discover_rdzv_host() {
  RDZV_HOST="$(echo "$NODE_LIST" | tr ',' '\n' | sort | head -1)"
}

# Sets GPUS_PER_NODE to a real, live nvidia-smi count on the CURRENT
# host -- the same discovery pattern already proven correct elsewhere in
# this codebase (classifier/cause_metrics.py's discover_gpu_count),
# generalized into the shell layer. Fails loudly on 0 GPUs found.
cluster_topology_discover_gpu_count() {
  GPUS_PER_NODE="$(nvidia-smi -L 2>/dev/null | wc -l)"
  if [ -z "$GPUS_PER_NODE" ] || [ "$GPUS_PER_NODE" -eq 0 ]; then
    echo "FATAL: nvidia-smi reported 0 GPUs on $(hostname) -- cannot proceed." >&2
    exit 1
  fi
}
