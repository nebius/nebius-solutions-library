#!/bin/bash
set -u
LABEL=${1:-check}
BASE=/root/P4d_clean/health
OUTDIR=/root/P4d_clean/health/runs/$LABEL
mkdir -p "$OUTDIR"
IMAGE="nvcr.io#nvidia/pytorch:25.01-py3"
MOUNTS="/usr/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu,/usr/lib64:/usr/lib64,/root:/root"

echo "--- queue check ---"
if [ -n "$(squeue -u soperatorchecks -h)" ]; then
  echo "ABORT: soperatorchecks job active"; exit 1
fi

echo "=== running matmul+sleep benchmark on both nodes ==="
srun --nodes=1 --ntasks=1 --gpus-per-node=8 -w worker-0 \
  --container-image="$IMAGE" --container-mounts="$MOUNTS" \
  python3 "$BASE/bench_all_gpus.py" > "$OUTDIR/bench_worker0.json" 2>"$OUTDIR/bench_worker0.err" &
P0=$!
srun --nodes=1 --ntasks=1 --gpus-per-node=8 -w worker-1 \
  --container-image="$IMAGE" --container-mounts="$MOUNTS" \
  python3 "$BASE/bench_all_gpus.py" > "$OUTDIR/bench_worker1.json" 2>"$OUTDIR/bench_worker1.err" &
P1=$!
wait $P0 $P1

echo "=== gathering NVML counters ==="
bash "$BASE/nvml_counters.sh" worker-0 > "$OUTDIR/nvml_worker0.csv"
bash "$BASE/nvml_counters.sh" worker-1 > "$OUTDIR/nvml_worker1.csv"

echo "=== REPORT: $LABEL ==="
python3 "$BASE/gpu_health_check.py" "$OUTDIR/bench_worker0.json" "$OUTDIR/nvml_worker0.csv" "$OUTDIR/bench_worker1.json" "$OUTDIR/nvml_worker1.csv"

scontrol update nodename=worker-0 state=resume >/dev/null 2>&1
scontrol update nodename=worker-1 state=resume >/dev/null 2>&1
