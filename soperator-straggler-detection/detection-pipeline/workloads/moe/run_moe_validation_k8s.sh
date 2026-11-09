#!/bin/bash
# run_moe_validation_k8s.sh -- K8s MoE staged straggler-detection validation.
#
# Runs the staged validation from DESIGN_NOTES §6.41 on K8s:
#   Phase 1: 3 healthy trials -- confirm zero crashes, PID-mismatch guard fires
#   Phase 2: 3 × 20ms fault trials -- confirm z-score < -5 on target rank
#   Phase 3: 3 × 100ms fault trials -- confirm z-score < -5 on target rank
#
# After each trial: copies dump files from pods, runs moe_signal_check_k8s.py.
#
# Usage: bash run_moe_validation_k8s.sh [steps] [fault_rank]
#   steps      -- training steps per trial (default: 500; Slurm used 10 for speed)
#   fault_rank -- target rank for fault injection (default: 4, same as Slurm)
#
# Prerequisites:
#   - source cluster_k8s.env
#   - Rebuilt Inspector plugin with proxy-event code (nm libnccl-profiler-inspector.so | grep ProxyPeerWait)
#   - Workspace PVC up to date (git checkout 96c6ab4f sources, make done)
#   - soperator NodeSet scaled to 0 (GPU nodes free)
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG="$(cd "$SCRIPT_DIR/../.." && pwd)"
K8S_DIR="$PKG/k8s"
NS="straggler-detection"
STEPS="${1:-500}"
FAULT_RANK="${2:-4}"
source "$PKG/cluster_k8s.env"

RESULTS_DIR="$PKG/var/moe_k8s_validation"
mkdir -p "$RESULTS_DIR"

log() { echo "$(date -u +%FT%TZ) [moe_validation] $*" | tee -a "$RESULTS_DIR/validation.log"; }

wait_for_training() {
    local run_name="$1" timeout_s="${2:-1800}"
    local start=$SECONDS done=0
    log "  Waiting for training to finish in job $run_name (timeout ${timeout_s}s)..."
    while [ $((SECONDS - start)) -lt "$timeout_s" ]; do
        local pods exit_count n_pods
        pods=$(kubectl get pods -n "$NS" -l "straggler.nebius.ai/run=$run_name" \
            -o jsonpath='{.items[*].metadata.name}' 2>/dev/null)
        n_pods=$(echo "$pods" | wc -w)
        exit_count=0
        for pod in $pods; do
            kubectl logs -n "$NS" "$pod" -c training --tail=5 2>/dev/null \
                | grep -q "TORCHRUN_EXIT:" && exit_count=$((exit_count+1)) || true
        done
        if [ "${n_pods:-0}" -gt 0 ] && [ "$exit_count" -ge "$n_pods" ]; then
            done=1; break
        fi
        sleep 10
    done
    [ "$done" -eq 0 ] && log "  WARNING: timeout waiting for TORCHRUN_EXIT"
}

copy_dumps() {
    local run_name="$1" trial_dir="$2"
    mkdir -p "$trial_dir/dumps"
    # Use aggregator container for dump copy: training container may already be dead
    # (aggregator sidecar stays alive longer and also has /inspector-dumps mounted).
    for pod in $(kubectl get pods -n "$NS" -l "straggler.nebius.ai/run=$run_name" \
        -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
        local node
        node=$(kubectl get pod -n "$NS" "$pod" -o jsonpath='{.spec.nodeName}' 2>/dev/null | tr -d '\n')
        # Try aggregator first (stays alive); fall back to training container
        for container in aggregator training; do
            kubectl exec -n "$NS" "$pod" -c "$container" -- \
                find /inspector-dumps -name "*.log" 2>/dev/null | \
                while read -r dfile; do
                    kubectl cp -n "$NS" "${pod}:${dfile}" \
                        "$trial_dir/dumps/${node}_$(basename "$dfile")" 2>/dev/null || true
                done && break
        done 2>/dev/null
        # Cross-process skip counts and PIN-PREFLIGHT from training logs
        kubectl logs -n "$NS" "$pod" -c training --tail=2000 2>/dev/null \
            | grep "cross-process context skipped\|ProxyOp cross-process\|PIN-PREFLIGHT\|NCCL-PROFILER-CHECK" \
            >> "$trial_dir/inspector_${pod}.txt" 2>/dev/null || true
    done
    local n_dump
    n_dump=$(find "$trial_dir/dumps" -name "*.log" 2>/dev/null | wc -l)
    log "  Copied $n_dump dump files to $trial_dir/dumps"
}

run_trial() {
    local phase="$1" trial_num="$2" fault_delay_us="$3"
    local trial_name="${phase}_t${trial_num}"
    local trial_dir="$RESULTS_DIR/$trial_name"
    mkdir -p "$trial_dir"

    local port=$((29600 + RANDOM % 100))
    log "START $trial_name  fault_delay_us=$fault_delay_us  fault_rank=$FAULT_RANK"

    MOE_FAULT_TARGET_RANK="$FAULT_RANK" \
    MOE_FAULT_DELAY_US="$fault_delay_us" \
    bash "$K8S_DIR/run_moe_k8s.sh" "$STEPS" "$port" "dump" "0" "" \
        > "$trial_dir/run_moe.log" 2>&1

    local run_name
    run_name=$(grep "^JOB_RUN_NAME=" "$trial_dir/run_moe.log" | tail -1 | cut -d= -f2)
    echo "$run_name" > "$trial_dir/job_name"

    if [ -z "$run_name" ]; then
        log "ERROR: could not get JOB_RUN_NAME for $trial_name"
        return 1
    fi

    # Wait for training completion
    wait_for_training "$run_name" 1800

    # Check exit codes
    local exits
    exits=$(kubectl logs -n "$NS" -l "straggler.nebius.ai/run=$run_name" -c training \
        --tail=5000 2>/dev/null | grep "TORCHRUN_EXIT:" | tr '\n' ' ')
    log "  TORCHRUN_EXIT lines: $exits"
    echo "$exits" > "$trial_dir/exit_codes.txt"

    # Copy dump files
    copy_dumps "$run_name" "$trial_dir"

    # Count cross-process skips from inspector logs in training output
    local skip_count
    skip_count=$(kubectl logs -n "$NS" -l "straggler.nebius.ai/run=$run_name" -c training \
        --tail=5000 2>/dev/null | grep -c "ProxyOp cross-process" || echo 0)
    log "  PID-mismatch guard: $skip_count log lines"
    echo "pid_skip_count=$skip_count" >> "$trial_dir/exit_codes.txt"

    # Run signal analysis on fault trials
    if [ "$fault_delay_us" -gt 0 ]; then
        local n_dumps
        n_dumps=$(find "$trial_dir/dumps" -name "*.log" 2>/dev/null | wc -l)
        if [ "$n_dumps" -gt 0 ]; then
            log "  Running moe_signal_check_k8s.py on $n_dumps dump files..."
            python3 -I "$SCRIPT_DIR/moe_signal_check_k8s.py" \
                "$trial_dir/dumps" --target-rank "$FAULT_RANK" --verbose \
                2>&1 | tee "$trial_dir/signal_analysis.txt"
            # Extract z-score for log
            local z
            z=$(grep "TARGET RANK $FAULT_RANK:" "$trial_dir/signal_analysis.txt" 2>/dev/null \
                | grep -oP "z=\K[+-]?[\d.]+")
            log "  Signal z-score rank $FAULT_RANK: ${z:-not found}"
        else
            log "  WARNING: no dump files copied -- signal analysis skipped"
        fi
    else
        # Healthy trial: check for proxy_peer_wait fields in dumps
        local ppw_count
        ppw_count=$(grep -rl "proxy_peer_wait" "$trial_dir/dumps/" 2>/dev/null | wc -l)
        log "  Dump files with proxy_peer_wait: $ppw_count"
    fi

    # Clean up K8s job
    kubectl delete job "$run_name" -n "$NS" --ignore-not-found 2>/dev/null || true
    kubectl delete svc "${run_name}-headless" -n "$NS" --ignore-not-found 2>/dev/null || true
    log "END $trial_name"
    echo "---"
}

log "=== MoE K8s Staged Validation ==="
log "STEPS=$STEPS  FAULT_RANK=$FAULT_RANK"
log "Plugin: $(ls -la $PKG/inspector-plugin/libnccl-profiler-inspector.so 2>/dev/null | awk '{print $5, $6, $7, $8}')"
log "Proxy symbols: $(nm $PKG/inspector-plugin/libnccl-profiler-inspector.so 2>/dev/null | grep -c ProxyPeerWait || echo 0)"
log "Results: $RESULTS_DIR"
echo ""

log "=== Phase 1: Healthy trials (3) ==="
for t in 1 2 3; do
    run_trial healthy "$t" 0
done

log "=== Phase 2: Fault trials 20ms (3) ==="
for t in 1 2 3; do
    run_trial f20ms "$t" 20000
done

log "=== Phase 3: Fault trials 100ms (3) ==="
for t in 1 2 3; do
    run_trial f100ms "$t" 100000
done

log "=== Validation COMPLETE ==="
log "Results directory: $RESULTS_DIR"
log "Signal analysis files: $(find $RESULTS_DIR -name signal_analysis.txt 2>/dev/null | sort | tr '\n' ' ')"
