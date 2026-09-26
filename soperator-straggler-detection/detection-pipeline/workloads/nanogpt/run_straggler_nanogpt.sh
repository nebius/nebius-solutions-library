#!/bin/bash
set -u
STEPS=$1
OUTDIR=$2
PORT=$3
DUMPDIR_W0=$4
DUMPDIR_W1=$5
SLEEP_MS=$6
TARGET_RANKS=$7
IMAGE="nvcr.io#nvidia/pytorch:25.01-py3"
# /tmp added (this session, disk-bound checkpoint-fault follow-up) -- the
# container's own base-image /tmp is tmpfs (RAM-backed, confirmed via a
# direct mount check), not the real host ext4 /dev/vda1 /tmp the eBPF
# storage detector's own validated fault mechanism requires; without this
# explicit bind, any real disk-bound write/read targeting /tmp from
# inside this container silently never touches a real block device.
MOUNTS="/usr/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu,/usr/lib64:/usr/lib64,/root:/root,/tmp:/tmp"

echo "### straggler-nanogpt steps=$STEPS outdir=$OUTDIR port=$PORT sleep_ms=$SLEEP_MS target_ranks=$TARGET_RANKS"
echo "--- queue check ---"
if [ -n "$(squeue -u soperatorchecks -h)" ]; then echo "ABORT: soperatorchecks job active"; exit 1; fi
if [ -n "$(squeue -u "$USER" -h)" ]; then echo "ABORT: existing job for $USER already queued/running"; exit 1; fi

mkdir -p "$OUTDIR"

srun --nodes=2 --ntasks=2 --ntasks-per-node=1 --gpus-per-node=8 -w worker-0,worker-1 \
  --container-image="$IMAGE" \
  --container-mounts="$MOUNTS" \
  --export=ALL,STRAGGLER_SLEEP_MS="$SLEEP_MS",STRAGGLER_TARGET_RANKS="$TARGET_RANKS" \
  bash -c 'if [ "$(hostname)" = "worker-0" ]; then DD='"$DUMPDIR_W0"'; else DD='"$DUMPDIR_W1"'; fi; bash /root/P20d_e2e_validation/p20k_mean_test/train_node_straggler.sh '"$STEPS $OUTDIR $PORT"' $DD' \
  > "$OUTDIR/full_output.log" 2>&1 &
echo $! > "$OUTDIR/srun_driver.pid"
disown
echo "launched, driver pid $(cat "$OUTDIR/srun_driver.pid")"
