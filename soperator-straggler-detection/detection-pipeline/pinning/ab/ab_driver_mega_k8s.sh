#!/bin/bash
# ab_driver_mega_k8s.sh -- K8s Megatron A/B/C pinning overhead measurement.
#
# Closes the methodology gap from ab_driver_k8s.sh (nanoGPT DDP):
#   - Uses Megatron TP=4/PP=1/DP=4 (same shape as §6.25 K8s validation)
#   - ALL arms use NCCL 2.32.3 (eliminates the NCCL-version confound)
#   - log-interval=1 → 300 steps = 300 log lines (20x more samples than nanoGPT)
#   - Otherwise identical methodology: 3 interleaved rounds, Welch t-test
#
# Three arms rotated across rounds:
#   off      -- Inspector OFF (INSPECTOR=off PIN_MODE=off), NCCL 2.32.3
#   on_nopin -- Inspector ON, PIN_MODE=off,  NCCL 2.32.3
#   on_full  -- Inspector ON, PIN_MODE=full, NCCL 2.32.3
#
# Usage: bash ab_driver_mega_k8s.sh TAG ROUNDS ITERS [SLEEP_MS [TARGET_RANKS]]
#   TAG        -- label (e.g. mega_ab_healthy)
#   ROUNDS     -- interleaved rounds (3 recommended)
#   ITERS      -- Megatron train-iters (300 recommended)
#   SLEEP_MS   -- fault delay ms (0 = healthy; default 0)
#   TARGET_RANKS -- fault rank (default "")
#
# Results: $RUNDIR/<TAG>/<arm>_r<R>/iters.txt (one ms/iter float per line)
# Analysis: python3 analyze_ab_mega_k8s.py TAG
set -uo pipefail

TAG="${1:?Usage: ab_driver_mega_k8s.sh TAG ROUNDS ITERS [SLEEP_MS [TARGET_RANKS]]}"
ROUNDS="${2:?}"
ITERS="${3:?}"
SLEEP_MS="${4:-0}"
TARGET_RANKS="${5:-}"
ARMS=(off on_nopin on_full)
N="${#ARMS[@]}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG="$(cd "$SCRIPT_DIR/../.." && pwd)"
K8S_DIR="$PKG/k8s"
NS="straggler-detection"

source "$PKG/cluster_k8s.env"
NCCL_LIB="${NCCL_232_LIB_PATH:-/workspace/nccl-2.32.3/build/lib}"

# Megatron shape: TP=4/PP=1/DP=4 (validated in §6.25; all gradient-sync groups role_n=4)
export MEGA_TP=4 MEGA_PP=1 MEGA_LAYERS=40 MEGA_HIDDEN=4096 MEGA_HEADS=32
export MEGA_SEQ=2048 MEGA_MBS=1 MEGA_GBS=16

RUNDIR="$PKG/var/ab_k8s"
mkdir -p "$RUNDIR/$TAG"

log() { echo "$(date -u +%FT%TZ) [ab_driver_mega_k8s] $*"; }

wait_for_no_jobs() {
    local attempts=0
    while true; do
        active=$(kubectl get jobs -n "$NS" -l straggler.nebius.ai/component=training \
            --field-selector='status.active=1' --no-headers 2>/dev/null | wc -l)
        [ "${active:-0}" -eq 0 ] && return 0
        attempts=$((attempts+1))
        [ "$((attempts % 6))" -eq 0 ] && log "  waiting for prior job to finish... (${attempts}x10s)"
        sleep 10
    done
}

run_one() {
    local arm="$1" round="$2"
    local name="${TAG}_r${round}_${arm}"
    local outdir="$RUNDIR/$TAG/${arm}_r${round}"
    mkdir -p "$outdir"

    local inspector pin port
    port=$((29500 + RANDOM % 400))
    case "$arm" in
        off)      inspector="off"; pin="off"  ;;
        on_nopin) inspector="on";  pin="off"  ;;
        on_full)  inspector="on";  pin="full" ;;
        *) echo "Unknown arm: $arm" >&2; return 1 ;;
    esac

    # All arms use NCCL 2.32.3 — eliminates the NCCL-version confound present in
    # the nanoGPT A/B (ab_driver_k8s.sh), where the off arm used host NCCL 2.25.1.
    log "START $name inspector=$inspector pin=$pin nccl=2.32.3"
    date -u +%s > "$outdir/t_start"

    INSPECTOR="$inspector" \
    NCCL_LIB_PATH="$NCCL_LIB" \
    SLEEP_MS="$SLEEP_MS" \
    TARGET_RANKS="$TARGET_RANKS" \
    bash "$K8S_DIR/run_megatron_k8s.sh" "$ITERS" "$port" "$pin" \
        "$SLEEP_MS" "$TARGET_RANKS" > "$outdir/run_mega_k8s.log" 2>&1

    local run_name
    run_name=$(grep "^JOB_RUN_NAME=" "$outdir/run_mega_k8s.log" | tail -1 | cut -d= -f2)
    echo "$run_name" > "$outdir/job_name"

    if [ -z "$run_name" ]; then
        log "ERROR: could not determine JOB_RUN_NAME for $name"
        return 1
    fi

    # Wait for training to finish by polling for TORCHRUN_EXIT in container logs.
    # kubectl wait --for=condition=Complete won't fire (aggregator sidecar stays alive).
    log "  Waiting for training to finish in job $run_name..."
    local _wait_attempts=0 _done=0
    while [ "$_done" -eq 0 ] && [ "$_wait_attempts" -lt 180 ]; do
        local _pods _exit_count _n_pods
        _pods=$(kubectl get pods -n "$NS" -l "straggler.nebius.ai/run=$run_name" \
            -o jsonpath='{.items[*].metadata.name}' 2>/dev/null)
        _n_pods=$(echo "$_pods" | wc -w)
        _exit_count=0
        for _pod in $_pods; do
            kubectl logs -n "$NS" "$_pod" -c training --tail=5 2>/dev/null \
                | grep -q "TORCHRUN_EXIT:" && _exit_count=$((_exit_count+1)) || true
        done
        if [ "${_n_pods:-0}" -gt 0 ] && [ "$_exit_count" -ge "$_n_pods" ]; then
            _done=1
        else
            _wait_attempts=$((_wait_attempts+1))
            [ "$((_wait_attempts % 6))" -eq 0 ] && \
                log "  still waiting... exit_count=${_exit_count}/${_n_pods} (${_wait_attempts}x10s)"
            sleep 10
        fi
    done
    [ "$_done" -eq 0 ] && log "  TIMEOUT waiting for TORCHRUN_EXIT"

    # CPU sampler for on_full arm — exec into each pod during training.
    if [ "$arm" = "on_full" ]; then
        for _pod in $(kubectl get pods -n "$NS" -l "straggler.nebius.ai/run=$run_name" \
            -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
            kubectl cp -n "$NS" "${_pod}:/outdir/pin_verify_$(kubectl get pod -n "$NS" "$_pod" \
                -o jsonpath='{.spec.nodeName}').log" \
                "$outdir/pin_verify_${_pod}.log" 2>/dev/null || true
        done
    fi

    # Extract Megatron iteration times ("elapsed time per iteration (ms): X.X")
    log "  Extracting iteration times for $name..."
    kubectl logs -n "$NS" -l "straggler.nebius.ai/run=$run_name" -c training \
        --tail=20000 2>/dev/null \
        | grep -oP "elapsed time per iteration \(ms\): \K[\d.]+" \
        > "$outdir/iters.txt" || true
    local n_iters
    n_iters=$(wc -l < "$outdir/iters.txt")
    log "  Got $n_iters iteration time records"

    # PIN-VERIFY log
    kubectl logs -n "$NS" -l "straggler.nebius.ai/run=$run_name" -c training \
        --tail=20000 2>/dev/null | grep "\[PIN-VERIFY\]\|\[PIN-PREFLIGHT\]" \
        > "$outdir/pin_verify_log.txt" 2>/dev/null || true

    date -u +%s > "$outdir/t_end"
    log "END $name (${n_iters} iter records)"

    kubectl delete job "$run_name" -n "$NS" --ignore-not-found 2>/dev/null || true
    kubectl delete svc "${run_name}-headless" -n "$NS" --ignore-not-found 2>/dev/null || true
}

log "=== Megatron K8s A/B/C pinning overhead (closes nanoGPT methodology gap) ==="
log "TAG=$TAG ROUNDS=$ROUNDS ITERS=$ITERS SHAPE=TP${MEGA_TP}/PP${MEGA_PP}/DP4"
log "SLEEP_MS=$SLEEP_MS TARGET_RANKS=${TARGET_RANKS:-none}"
log "NCCL: ALL arms use $NCCL_LIB (eliminates NCCL-version confound)"
log "Output: $RUNDIR/$TAG"

for ((r=1; r<=ROUNDS; r++)); do
    for ((i=0; i<N; i++)); do
        arm="${ARMS[$(( (i + r - 1) % N ))]}"
        wait_for_no_jobs
        run_one "$arm" "$r"
    done
done

log "ALL_DONE -- run: python3 $SCRIPT_DIR/analyze_ab_mega_k8s.py $TAG"
