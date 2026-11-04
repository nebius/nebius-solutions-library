#!/bin/bash
# pin_env.sh -- SOURCE this from any node-level launcher (NEW, UNVALIDATED). The shared pinning wrapper.
#
#   source /path/to/pinning/pin_env.sh        # needs: GPUS_PER_NODE; optional: OUTDIR, HN
#   pin_prepare                               # resolves mode, builds the interposer if needed, prints preflight, starts verifier
#   torchrun ... "${PIN_WRAP[@]}" train.py ...   # PIN_WRAP is empty when pinning is off/unavailable
#
# Inputs (env):  PIN_MODE=auto|full|dump|off (default auto)   PIN_CORES_PER_ROLE (1)   PIN_LOG_DIR (default $OUTDIR/pinlogs)
#                NCCL_OVERRIDE_DIR (else pinning/.nccl_override_dir written by nccl/fetch_build_nccl.sh)
#                PIN_VERIFY_SAMPLE / PIN_VERIFY_SAMPLE_AFTER (seconds; per-thread drift sampling, 0=off)
# Never aborts the job: any problem degrades to an unpinned run with a loud [PIN][WARN] line.
_PIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

pin_prepare() {
  PIN_WRAP=()
  local want="${PIN_MODE:-auto}" hn="${HN:-$(hostname)}"
  export PIN_LOG_DIR="${PIN_LOG_DIR:-${OUTDIR:-/tmp}/pinlogs}"; mkdir -p "$PIN_LOG_DIR"
  # An explicit NCCL_OVERRIDE_DIR always applies (even with pinning off: it selects the NCCL version, which A/B arms rely on).
  _pin_apply_override() {
    [ -n "${NCCL_OVERRIDE_DIR:-}" ] || return 0
    case ":${LD_LIBRARY_PATH:-}:" in *":$NCCL_OVERRIDE_DIR:"*) ;; *) export LD_LIBRARY_PATH="$NCCL_OVERRIDE_DIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}";; esac
  }
  _pin_apply_override
  [ "$want" = "off" ] && { echo "[PIN-PREFLIGHT] host=$hn mode=off (PIN_MODE=off)"; return 0; }
  # Only when pinning is on: fall back to the dir recorded by fetch_build_nccl.sh (if it still exists).
  if [ -z "${NCCL_OVERRIDE_DIR:-}" ] && [ -s "$_PIN_DIR/.nccl_override_dir" ]; then
    local d; d="$(head -1 "$_PIN_DIR/.nccl_override_dir")"; [ -e "$d/libnccl.so.2" ] && { export NCCL_OVERRIDE_DIR="$d"; _pin_apply_override; }
  fi

  local _R; _R="$(PIN_MODE="$want" python3 -I "$_PIN_DIR/pin_resolve.py" 2>/dev/null)" \
    || { echo "[PIN][WARN] host=$hn pin_resolve.py failed -- running UNPINNED"; return 0; }
  eval "$_R"
  export PIN_MODE_RESOLVED PIN_NCCL_LIB="$PIN_NCCL_LIB_RESOLVED"
  local lib="cached"
  if [ "$PIN_MODE_RESOLVED" != "off" ]; then
    { [ -s "$_PIN_DIR/libthread_pin_new.so" ] && [ "$_PIN_DIR/libthread_pin_new.so" -nt "$_PIN_DIR/thread_pin_new.c" ]; } || lib="built-now"
    PIN_LIB_PATH="$(bash "$_PIN_DIR/build.sh" --if-needed 2>"$PIN_LOG_DIR/build_$hn.log")" \
      || { echo "[PIN][WARN] host=$hn cannot obtain libthread_pin_new.so (see $PIN_LOG_DIR/build_$hn.log) -- running UNPINNED; run pinning/build.sh where gcc exists"; export PIN_MODE_RESOLVED=off; lib="MISSING"; PIN_REASON="interposer unavailable -> UNPINNED (NCCL probe alone had said: $PIN_REASON)"; }
    export PIN_LIB_PATH
  fi
  local roles=dump; [ "$PIN_MODE_RESOLVED" = "full" ] && roles=dump,profiler
  local reserved=""
  [ "$PIN_MODE_RESOLVED" != "off" ] && reserved="$(PIN_ROLES=$roles python3 -I "$_PIN_DIR/rank_setup.py" --local-world "${GPUS_PER_NODE:-8}" --emit-env 2>/dev/null | sed -n "s/^export PIN_RESERVED_CPUS='\(.*\)'/\1/p")"
  echo "[PIN-PREFLIGHT] host=$hn requested=$want resolved=$PIN_MODE_RESOLVED nccl=${PIN_NCCL_VERSION:-?} lib=${PIN_NCCL_LIB:-none} interposer=$lib cores_per_role=${PIN_CORES_PER_ROLE:-1} reserved_cpus=${reserved:-none} reason: $PIN_REASON" | tee -a "$PIN_LOG_DIR/launch_$hn.log"
  [ "$PIN_MODE_RESOLVED" = "off" ] && return 0

  export PIN_MODE="$PIN_MODE_RESOLVED"        # children see the concrete mode, never 'auto'
  PIN_WRAP=(--no-python bash "$_PIN_DIR/rank_launch.sh" python3)
  ( python3 -I "$_PIN_DIR/verify_pinning.py" --log-dir "$PIN_LOG_DIR" --expect-ranks "${GPUS_PER_NODE:-8}" --roles "$roles" --wait 900 \
      --sample "${PIN_VERIFY_SAMPLE:-0}" --sample-after "${PIN_VERIFY_SAMPLE_AFTER:-0}" --reserved "$reserved" \
      > "${OUTDIR:-$PIN_LOG_DIR}/pin_verify_$hn.log" 2>&1 & )
}
