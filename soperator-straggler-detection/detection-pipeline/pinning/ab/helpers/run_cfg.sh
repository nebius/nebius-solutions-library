# usage: source run_cfg.sh; run_one NAME ITERS INSPECTOR(on|off) PIN_MODE(off|dump|full) NCCL(host|2323) [extra env...]
PKG=/root/inspector-audit/repo/soperator-straggler-detection/detection-pipeline
run_one() {
  local name=$1 iters=$2 insp=$3 pin=$4 nccl=$5; shift 5
  local out=/root/inspector-audit/runs/$name port=$((29500 + RANDOM % 400))
  rm -rf "$out"; mkdir -p "$out"
  while [ -n "$(squeue -h)" ]; do sleep 10; done   # wait for health checks / other jobs
  date -u +%s > $out/t_start
  local ov=""; [ "$nccl" = 2323 ] && ov=/root/nccl-2.32.3-src/build/lib
  ( export MEGA_TP=4 MEGA_PP=2 MEGA_LAYERS=${MEGA_LAYERS:-40} MEGA_HIDDEN=${MEGA_HIDDEN:-4096} MEGA_HEADS=${MEGA_HEADS:-32} MEGA_SEQ=${MEGA_SEQ:-2048} MEGA_MBS=1 MEGA_GBS=${MEGA_GBS:-32} \
           SRUN_EXTRA="--exclusive --cpus-per-task=${CPUS_PER_TASK:-128} --cpu-bind=none" INSPECTOR=$insp PIN_MODE=$pin NCCL_OVERRIDE_DIR=$ov "$@"
    cd $PKG/workloads/megatron && bash run_megatron_tp4pp4dp3.sh $iters $out $port ${DUMP_BASE:-/tmp/insp_dump_$name} > $out/launch.log 2>&1
    # launcher backgrounds srun; wait for it
    while kill -0 $(cat $out/srun_driver.pid 2>/dev/null) 2>/dev/null; do sleep 5; done; date -u +%s > $out/t_end )
}
