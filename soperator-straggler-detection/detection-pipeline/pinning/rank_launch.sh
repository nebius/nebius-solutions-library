#!/bin/bash
# rank_launch.sh -- NEW, UNVALIDATED per-rank launcher for the pinning mechanism.
# Used as:  torchrun ... --no-python bash rank_launch.sh python3 train.py ARGS   (torchrun sets LOCAL_RANK / LOCAL_WORLD_SIZE)
# Normally invoked via pin_env.sh (which resolves the mode once per node). Standalone use also works:
#   PIN_MODE = auto | full | dump | off (default auto)  -- see pin_resolve.py for how 'auto'/'full' are decided
#   PIN_CORES_PER_ROLE (default 1)  PIN_LOG_DIR (required)  PIN_NCCL_LIB / NCCL_OVERRIDE_DIR (which NCCL to probe)
# Any failure degrades to running the training command UNPINNED with a loud warning; it never blocks the job.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${PIN_MODE:-auto}"
[ "$MODE" = "off" ] && exec "$@"
LOGD="${PIN_LOG_DIR:-/tmp}"; mkdir -p "$LOGD" 2>/dev/null
unpinned() { echo "[PIN][WARN] host=$(hostname) pid=$$ local_rank=${LOCAL_RANK:-?} $1 -- running UNPINNED; this run is NOT dedicated" | tee -a "$LOGD/launch_$(hostname).log" >&2; exec "$@"; }
# resolve (skipped when pin_env.sh already did it and exported a concrete mode)
if [ "$MODE" = "auto" ] || [ "$MODE" = "full" ]; then
  _R="$(PIN_MODE="$MODE" python3 -I "$HERE/pin_resolve.py")" || unpinned "pin_resolve failed"
  eval "$_R"; MODE="$PIN_MODE_RESOLVED"; export PIN_NCCL_LIB="${PIN_NCCL_LIB_RESOLVED:-${PIN_NCCL_LIB:-}}"
fi
[ "$MODE" = "off" ] && exec "$@"
case "$MODE" in dump) export PIN_ROLES=dump ;; full) export PIN_ROLES=dump,profiler ;; *) unpinned "bad PIN_MODE=$MODE" ;; esac
LIB="${PIN_LIB_PATH:-}"
[ -s "$LIB" ] || LIB="$(bash "$HERE/build.sh" --if-needed 2>>"$LOGD/build_$(hostname).log")" || unpinned "libthread_pin_new.so unavailable and cannot be built"
_ENV="$(python3 -I "$HERE/rank_setup.py" --emit-env)" || unpinned "rank_setup failed"
eval "$_ENV"
export PIN_ENABLE=1 PIN_LOG_DIR="$LOGD"
echo "[PIN] host=$(hostname) pid=$$ local_rank=${LOCAL_RANK:-?} mode=$MODE train_cpus=$TRAIN_CPUS dump=$PIN_DUMP_CPUS profiler=$PIN_PROFILER_CPUS" >> "$LOGD/launch_$(hostname).log"
export LD_PRELOAD="$LIB${LD_PRELOAD:+:$LD_PRELOAD}"
exec taskset -c "$TRAIN_CPUS" "$@"
