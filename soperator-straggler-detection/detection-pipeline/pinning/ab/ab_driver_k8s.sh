#!/bin/bash
# ab_driver_k8s.sh -- interleaved A/B/C CPU-pinning overhead measurement for K8s.
#
# Three arms rotated across rounds (round r starts at arm r mod 3):
#   off      -- Inspector completely OFF (true baseline, no plugin loaded)
#   on_nopin -- Inspector ON, PIN_MODE=off (unpinned; NCCL 2.32.3 so profiler thread exists)
#   on_full  -- Inspector ON, PIN_MODE=full (both dump + profiler threads dedicated)
#
# Usage: bash ab_driver_k8s.sh TAG ROUNDS STEPS [SLEEP_MS [TARGET_RANKS]]
#   TAG        -- label for this run (e.g. k8s_ab_healthy)
#   ROUNDS     -- number of interleaved rounds (3 recommended; 9 jobs per 3 rounds)
#   STEPS      -- torchrun iterations per job (300 recommended)
#   SLEEP_MS   -- fault injection delay ms (0 = healthy; default 0)
#   TARGET_RANKS -- ranks to inject fault into (e.g. "1"; default "" = no fault)
#
# Output per job: raw iteration times saved to $RUNDIR/<TAG>/<arm>_r<R>/iters.txt
# After all runs: call analyze_ab_k8s.py TAG for statistics.
#
# Parity with Slurm's ab_driver.sh:
#   SAME: round-based arm rotation (round r starts at arm (r-1) mod N),
#         Welch t-test in analyze_ab_k8s.py, 20-iteration warmup skip,
#         CPU sampler during on_full runs, /proc affinity check
#   DIFFERENT: launch via kubectl (not srun); queue-wait via kubectl wait
#              (not squeue); contamination check via kubectl (not sacct);
#              workload is nanoGPT DDP (not Megatron TP4/PP2/DP2)
set -uo pipefail

TAG="${1:?Usage: ab_driver_k8s.sh TAG ROUNDS STEPS [SLEEP_MS [TARGET_RANKS]]}"
ROUNDS="${2:?}"
STEPS="${3:?}"
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

RUNDIR="$PKG/var/ab_k8s"
mkdir -p "$RUNDIR/$TAG"

log() { echo "$(date -u +%FT%TZ) [ab_driver_k8s] $*"; }

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

    local inspector pin nccl_lib port
    port=$((29500 + RANDOM % 400))
    case "$arm" in
        off)
            inspector="off"; pin="off"; nccl_lib=""
            ;;
        on_nopin)
            inspector="on"; pin="off"; nccl_lib="$NCCL_LIB"
            ;;
        on_full)
            inspector="on"; pin="full"; nccl_lib="$NCCL_LIB"
            ;;
        *) echo "Unknown arm: $arm" >&2; return 1 ;;
    esac

    log "START $name inspector=$inspector pin=$pin nccl_lib=${nccl_lib:-none}"
    date -u +%s > "$outdir/t_start"

    INSPECTOR="$inspector" \
    SLEEP_MS="$SLEEP_MS" \
    TARGET_RANKS="$TARGET_RANKS" \
    NCCL_LIB_PATH="$nccl_lib" \
    bash "$K8S_DIR/run_k8s.sh" "$STEPS" "$port" "$pin" > "$outdir/run_k8s.log" 2>&1
    local run_name
    run_name=$(grep "^JOB_RUN_NAME=" "$outdir/run_k8s.log" | tail -1 | cut -d= -f2)
    echo "$run_name" > "$outdir/job_name"

    if [ -z "$run_name" ]; then
        log "ERROR: could not determine JOB_RUN_NAME for $name"
        return 1
    fi

    # For on_full arm: start cpu_sampler.py inside each training pod as background process.
    # We exec into each pod and run the sampler; output saved to outdir via shared workspace.
    local sampler_pids=()
    if [ "$arm" = "on_full" ]; then
        log "  Starting CPU sampler on training pods for $name..."
        # Wait a bit for pods to be Ready before exec
        kubectl wait --for=condition=Ready pods -n "$NS" \
            -l "straggler.nebius.ai/run=$run_name" --timeout=120s 2>/dev/null || true
        for pod in $(kubectl get pods -n "$NS" -l "straggler.nebius.ai/run=$run_name" \
            -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
            local pod_out="$outdir/cpu_${pod}.jsonl"
            (kubectl exec -n "$NS" "$pod" -c training -- \
                python3 -I "$PKG/pinning/cpu_sampler.py" "/outdir/cpu_${pod}.jsonl" \
                > /dev/null 2>&1) &
            sampler_pids+=($!)
        done
    fi

    # Wait for training containers to exit (not kubectl wait --for=condition=Complete:
    # the aggregator sidecar stays alive indefinitely, so the Job never reaches Complete.
    # Instead, poll for TORCHRUN_EXIT in training container logs from each pod.)
    log "  Waiting for training to finish in job $run_name..."
    local _wait_attempts=0 _done=0
    while [ "$_done" -eq 0 ] && [ "$_wait_attempts" -lt 180 ]; do
        local _pods _exit_count
        _pods=$(kubectl get pods -n "$NS" -l "straggler.nebius.ai/run=$run_name" \
            -o jsonpath='{.items[*].metadata.name}' 2>/dev/null)
        _exit_count=0
        for _pod in $_pods; do
            kubectl logs -n "$NS" "$_pod" -c training --tail=5 2>/dev/null \
                | grep -q "TORCHRUN_EXIT:" && _exit_count=$((_exit_count+1)) || true
        done
        local _n_pods
        _n_pods=$(echo "$_pods" | wc -w)
        if [ "${_n_pods:-0}" -gt 0 ] && [ "$_exit_count" -ge "$_n_pods" ]; then
            _done=1
        else
            _wait_attempts=$((_wait_attempts+1))
            [ "$((_wait_attempts % 6))" -eq 0 ] && \
                log "  still waiting... exit_count=${_exit_count}/${_n_pods} (${_wait_attempts}x10s)"
            sleep 10
        fi
    done
    [ "$_done" -eq 0 ] && log "  TIMEOUT waiting for TORCHRUN_EXIT after ${_wait_attempts} attempts"

    # Kill samplers
    for pid in "${sampler_pids[@]:-}"; do
        kill "$pid" 2>/dev/null || true
    done

    # Retrieve CPU sampler output from pods (if on_full)
    if [ "$arm" = "on_full" ]; then
        for pod in $(kubectl get pods -n "$NS" -l "straggler.nebius.ai/run=$run_name" \
            -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
            kubectl cp -n "$NS" "${pod}:/outdir/cpu_${pod}.jsonl" \
                "$outdir/cpu_${pod}.jsonl" 2>/dev/null || true
        done
    fi

    # Retrieve /proc affinity verification log (if on_full or on_nopin, pinning ran)
    if [ "$arm" = "on_full" ]; then
        for pod in $(kubectl get pods -n "$NS" -l "straggler.nebius.ai/run=$run_name" \
            -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
            kubectl cp -n "$NS" "${pod}:/outdir/pin_verify_$(kubectl get pod -n "$NS" "$pod" \
                -o jsonpath='{.spec.nodeName}').log" \
                "$outdir/pin_verify_${pod}.log" 2>/dev/null || true
        done
    fi

    # Extract iteration times from training container logs
    log "  Extracting iteration times for $name..."
    kubectl logs -n "$NS" -l "straggler.nebius.ai/run=$run_name" -c training \
        --tail=10000 2>/dev/null \
        | grep -oP "time \K[\d.]+(?=ms)" > "$outdir/iters.txt" || true
    local n_iters
    n_iters=$(wc -l < "$outdir/iters.txt")
    log "  Got $n_iters iteration time records for $name"

    # Retrieve pin verify log directly from pod logs
    kubectl logs -n "$NS" -l "straggler.nebius.ai/run=$run_name" -c training \
        --tail=10000 2>/dev/null | grep "\[PIN-VERIFY\]\|\[PIN-PREFLIGHT\]" \
        > "$outdir/pin_verify_log.txt" 2>/dev/null || true

    date -u +%s > "$outdir/t_end"
    log "END $name (${n_iters} iter records)"

    # Cleanup headless service and job
    kubectl delete job "$run_name" -n "$NS" --ignore-not-found 2>/dev/null || true
    kubectl delete svc "${run_name}-headless" -n "$NS" --ignore-not-found 2>/dev/null || true
}

log "=== K8s A/B/C pinning overhead measurement ==="
log "TAG=$TAG ROUNDS=$ROUNDS STEPS=$STEPS ARMS=${ARMS[*]}"
log "SLEEP_MS=$SLEEP_MS TARGET_RANKS=${TARGET_RANKS:-none}"
log "NCCL_LIB=$NCCL_LIB"
log "Output: $RUNDIR/$TAG"

for ((r=1; r<=ROUNDS; r++)); do
    for ((i=0; i<N; i++)); do
        arm="${ARMS[$(( (i + r - 1) % N ))]}"
        wait_for_no_jobs
        run_one "$arm" "$r"
    done
done

log "ALL_DONE -- run: python3 $SCRIPT_DIR/analyze_ab_k8s.py $TAG"
