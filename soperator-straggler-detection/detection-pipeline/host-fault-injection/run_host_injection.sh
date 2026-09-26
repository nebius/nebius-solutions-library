#!/bin/bash
set -u
MODE=$1  # cpu_inject or healthy
STEPS=$2
OUTDIR=$3
PORT=$4
BASE=/root/P4d_clean
IMAGE="nvcr.io#nvidia/pytorch:25.01-py3"
MOUNTS="/usr/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu,/usr/lib64:/usr/lib64,/root:/root"

echo "### mode=$MODE steps=$STEPS outdir=$OUTDIR port=$PORT"

echo "--- queue check ---"
if [ -n "$(squeue -u soperatorchecks -h)" ]; then
  echo "ABORT: soperatorchecks job active"; exit 1
fi
if [ -n "$(squeue -u "$USER" -h)" ]; then
  echo "ABORT: existing job for $USER already queued/running"; exit 1
fi

mkdir -p "$OUTDIR"

srun --nodes=2 --ntasks=2 --ntasks-per-node=1 --gpus-per-node=8 -w worker-0,worker-1 \
  --container-image="$IMAGE" \
  --container-mounts="$MOUNTS" \
  bash "$BASE/train_node.sh" "$STEPS" "$OUTDIR" "$PORT" 0 4 \
  > "$OUTDIR/full_output.log" 2>&1 &
SRUN_PID=$!

if [ "$MODE" = "cpu_inject" ]; then
  sleep 10
  ssh -o BatchMode=yes worker-0 'nohup /tmp/cpu_burn.sh 600 > /tmp/cpu_burn.log 2>&1 & echo $! > /tmp/cpu_burn.pid'
  echo "CPU burner started on worker-0"
fi

wait "$SRUN_PID"
SRUN_EC=$?
echo "srun exit: $SRUN_EC"
grep -H TORCHRUN_EXIT "$OUTDIR/full_output.log"

if [ "$MODE" = "cpu_inject" ]; then
  ssh -o BatchMode=yes worker-0 'kill $(cat /tmp/cpu_burn.pid) 2>/dev/null; pkill -f "while true" 2>/dev/null; rm -f /tmp/cpu_burn.pid'
  echo "CPU burner stopped"
fi

scontrol update nodename=worker-0 state=resume >/dev/null 2>&1
scontrol update nodename=worker-1 state=resume >/dev/null 2>&1

ssh -o BatchMode=yes worker-0 'rm -rf /tmp/p4d_inspector_dumps' >/dev/null 2>&1
ssh -o BatchMode=yes worker-1 'rm -rf /tmp/p4d_inspector_dumps' >/dev/null 2>&1
