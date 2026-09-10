#!/bin/bash

#SBATCH --job-name=checkpointing-demo
#SBATCH --output=results/checkpointing-demo.%j.out
#SBATCH --error=results/checkpointing-demo.%j.out
#SBATCH --nodes=2
#SBATCH --gpus-per-node=8
#SBATCH --ntasks-per-node=8
#SBATCH --cpus-per-task=14
# Bound the example allocation; override with sbatch --time for a longer run.
#SBATCH --time=00:30:00
# Requeue on node failure/preemption; the job auto-resumes from the latest
# complete checkpoint in Object Storage (see train_fsdp.py).
#SBATCH --requeue
# Append to the log on requeue instead of truncating it.
#SBATCH --open-mode=append
# Ask Slurm to signal the job steps (the training ranks trap USR1) 120s before
# a scheduled kill so they can commit the current step before exit. No "B:"
# prefix: that would signal only the batch shell, never reaching the ranks.
#SBATCH --signal=USR1@120

set -euo pipefail

# Slurm copies batch scripts to its spool dir, so resolve paths from the
# submission directory (submit this job from the checkpointing directory).
CHECKPOINTING_DIR="${SLURM_SUBMIT_DIR:-/opt/slurm-test/checkpointing}"
export PATH="${CHECKPOINTING_DIR}/bin:${PATH}"

# Nebius Object Storage credentials, endpoint, and bucket, delivered by the
# checkpoints_access module. AWS_* exports inside the file are SDK compatibility
# inputs; workload-facing settings use NEBIUS_* names.
set -a
# shellcheck disable=SC1091 # rendered by the checkpoint access module
source /etc/nebius-checkpoints.env
set +a

MASTER_ADDR=$(scontrol show hostnames "$SLURM_JOB_NODELIST" | sed -n '1p')
[ -n "${MASTER_ADDR}" ] || { echo "ERROR: could not resolve the Slurm master node" >&2; exit 1; }
export MASTER_ADDR
export MASTER_PORT="${MASTER_PORT:-$((20000 + SLURM_JOB_ID % 20000))}"

# One task per GPU; torch reads its distributed config from these variables.
srun --kill-on-bad-exit=1 bash -c "
  export RANK=\$SLURM_PROCID
  export WORLD_SIZE=\$SLURM_NTASKS
  export LOCAL_RANK=\$SLURM_LOCALID
  exec '${CHECKPOINTING_DIR}/venv/bin/python' '${CHECKPOINTING_DIR}/train_fsdp.py' \
    --save-every-seconds 120 \
    ${TRAIN_ARGS:-}
"
