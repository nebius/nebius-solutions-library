#!/bin/bash
# env_sampler.sh — environment telemetry collector for straggler-detection sessions
#
# PURPOSE
# -------
# Runs every 30 seconds via kubectl exec into live training pods.  Collects host-level
# and GPU-level metrics whose timestamps align with [STRAGGLER-INCIDENT] / FALCON_STAGE3_RESULT
# event ts= fields.  Output is used for post-hoc autopsy of anomalous events (e.g. the R3
# Megatron 32.2x healthy event and the iters 10,000–17,000 cluster-wide episode) and for
# future injected-diffuse-slowdown labelling.
#
# NO privileged pods.  NO new container images.  Uses kubectl exec into whichever training
# pods are live at sample time.
#
# USAGE
# -----
#   # From the control machine (kubectl configured):
#   source detection-pipeline/cluster_k8s.env
#   nohup bash k8s/alert_layer/env_sampler.sh >> /tmp/env_sampler.log 2>&1 &
#   echo "sampler PID=$!"
#
#   # To stop:  kill <PID>
#
# OUTPUT FORMAT (one metric per line, tab-separated):
#   TIMESTAMP_UTC<TAB>POD<TAB>METRIC<TAB>VALUE
#
# Timestamps use the same format as Falcon/Inspector ts= fields:
#   2026-11-13T14:35:00.123Z
#
# METRICS COLLECTED PER POD (all via /proc; no root required)
#   net_tcp_retrans_segs      — /proc/net/snmp Tcp RetransSegs (cumulative counter)
#   net_<iface>_rx_errors     — /proc/net/dev per-interface receive errors (cumulative)
#   net_<iface>_rx_drops      — /proc/net/dev per-interface receive drops (cumulative)
#   net_<iface>_tx_errors     — /proc/net/dev per-interface transmit errors (cumulative)
#   net_<iface>_tx_drops      — /proc/net/dev per-interface transmit drops (cumulative)
#   loadavg_1m                — /proc/loadavg 1-minute load average
#   loadavg_5m                — /proc/loadavg 5-minute load average
#   loadavg_15m               — /proc/loadavg 15-minute load average
#   cpu_steal_jiffies         — /proc/stat cpu field 8 (steal time, cumulative jiffies)
#   gpu<N>_clocks_gr_mhz      — nvidia-smi graphics clock (MHz) per GPU
#   gpu<N>_clocks_mem_mhz     — nvidia-smi memory clock (MHz) per GPU
#   gpu<N>_power_draw_w       — nvidia-smi power draw (W) per GPU
#   gpu<N>_temperature_c      — nvidia-smi temperature (C) per GPU
#   gpu<N>_throttle_reasons   — nvidia-smi clocks_throttle_reasons.active (hex bitmask)
#
# NOTE: nvidia-smi is called once per pod per sample interval (one row per GPU).
# If the training container does not have nvidia-smi on PATH, GPU metrics are skipped
# with a warning and proc-level metrics continue normally.

set -uo pipefail

CLUSTER_ENV="${CLUSTER_ENV:-$(dirname "${BASH_SOURCE[0]}")/../../cluster_k8s.env}"
if [ -f "$CLUSTER_ENV" ]; then
    source "$CLUSTER_ENV"
fi

NS="${K8S_NAMESPACE:-straggler-detection}"
INTERVAL="${SAMPLER_INTERVAL_S:-30}"
# Label selector that matches training pods across all training job types
# (megatron, ddp, fsdp, moe).  Adjust if your label scheme differs.
POD_LABEL="${SAMPLER_POD_LABEL:-straggler.nebius.ai/component=training}"
# Container to exec into (training workload container)
CONTAINER="${SAMPLER_CONTAINER:-training}"
# Output file (default: stdout).  Set SAMPLER_OUT to redirect.
OUT="${SAMPLER_OUT:-/dev/stdout}"

# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------

now_utc() {
    date -u +%Y-%m-%dT%H:%M:%S.%3NZ
}

emit() {
    # emit TS POD METRIC VALUE
    printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" >> "$OUT"
}

warn() {
    printf '[env_sampler WARN %s] %s\n' "$(now_utc)" "$*" >&2
}

# Run a command inside a pod and capture output.  Returns 1 on exec failure.
pod_exec() {
    local pod="$1"
    shift
    kubectl exec -n "$NS" "$pod" -c "$CONTAINER" -- "$@" 2>/dev/null
}

# ------------------------------------------------------------------
# per-pod sampling
# ------------------------------------------------------------------

sample_pod() {
    local pod="$1"
    local ts
    ts="$(now_utc)"

    # --- /proc/net/snmp: TCP RetransSegs ---
    local snmp
    snmp="$(pod_exec "$pod" cat /proc/net/snmp 2>/dev/null)" || { warn "$pod: /proc/net/snmp unavailable"; return; }
    # Format: header line "Tcp: ... RetransSegs ..." followed by value line
    local retrans
    retrans="$(printf '%s\n' "$snmp" | awk '
        /^Tcp:/ {
            if (!header_done) { split($0,H); header_done=1 }
            else {
                split($0,V)
                for (i=1;i<=length(H);i++) if (H[i]=="RetransSegs") { print V[i]; exit }
            }
        }' 2>/dev/null)"
    [ -n "$retrans" ] && emit "$ts" "$pod" "net_tcp_retrans_segs" "$retrans"

    # --- /proc/net/dev: errors and drops per interface ---
    local netdev
    netdev="$(pod_exec "$pod" cat /proc/net/dev 2>/dev/null)" || warn "$pod: /proc/net/dev unavailable"
    if [ -n "$netdev" ]; then
        # Format: iface: rx_bytes rx_packets rx_errs rx_drop ... tx_bytes tx_packets tx_errs tx_drop ...
        # Fields (1-indexed after splitting on whitespace, iface col stripped of colon):
        #   1=iface 2=rx_bytes 3=rx_pkts 4=rx_errs 5=rx_drop 6=rx_fifo 7=rx_frame 8=rx_comp 9=rx_mcast
        #   10=tx_bytes 11=tx_pkts 12=tx_errs 13=tx_drop ...
        printf '%s\n' "$netdev" | awk 'NR>2 {
            gsub(/:/, "", $1)
            if ($1 == "lo") next
            print $1, $4, $5, $12, $13
        }' | while read -r iface rx_err rx_drop tx_err tx_drop; do
            emit "$ts" "$pod" "net_${iface}_rx_errors" "$rx_err"
            emit "$ts" "$pod" "net_${iface}_rx_drops"  "$rx_drop"
            emit "$ts" "$pod" "net_${iface}_tx_errors" "$tx_err"
            emit "$ts" "$pod" "net_${iface}_tx_drops"  "$tx_drop"
        done
    fi

    # --- /proc/loadavg ---
    local lavg
    lavg="$(pod_exec "$pod" cat /proc/loadavg 2>/dev/null)" || warn "$pod: /proc/loadavg unavailable"
    if [ -n "$lavg" ]; then
        read -r la1 la5 la15 _ _ <<< "$lavg"
        emit "$ts" "$pod" "loadavg_1m"  "$la1"
        emit "$ts" "$pod" "loadavg_5m"  "$la5"
        emit "$ts" "$pod" "loadavg_15m" "$la15"
    fi

    # --- /proc/stat: CPU steal (field 9 on the cpu line) ---
    local cpustat
    cpustat="$(pod_exec "$pod" awk '/^cpu / {print $9; exit}' /proc/stat 2>/dev/null)" || \
        warn "$pod: /proc/stat unavailable"
    [ -n "$cpustat" ] && emit "$ts" "$pod" "cpu_steal_jiffies" "$cpustat"

    # --- nvidia-smi per-GPU ---
    local smi_out
    smi_out="$(pod_exec "$pod" nvidia-smi \
        --query-gpu=index,clocks.gr,clocks.mem,power.draw,temperature.gpu,clocks_throttle_reasons.active \
        --format=csv,noheader 2>/dev/null)" || {
        warn "$pod: nvidia-smi unavailable — skipping GPU metrics"
        return 0
    }
    # CSV columns: index, clocks.gr [MHz], clocks.mem [MHz], power.draw [W], temperature.gpu [C], throttle_reasons
    printf '%s\n' "$smi_out" | while IFS=', ' read -r gpu_idx clk_gr clk_mem pwr tmp throttle; do
        # Strip units (nvidia-smi appends " MHz", " W", " C")
        clk_gr="${clk_gr%% *}"
        clk_mem="${clk_mem%% *}"
        pwr="${pwr%% *}"
        tmp="${tmp%% *}"
        # throttle may contain spaces in some nvidia-smi versions; collapse
        throttle="${throttle// /}"
        emit "$ts" "$pod" "gpu${gpu_idx}_clocks_gr_mhz"    "$clk_gr"
        emit "$ts" "$pod" "gpu${gpu_idx}_clocks_mem_mhz"   "$clk_mem"
        emit "$ts" "$pod" "gpu${gpu_idx}_power_draw_w"      "$pwr"
        emit "$ts" "$pod" "gpu${gpu_idx}_temperature_c"     "$tmp"
        emit "$ts" "$pod" "gpu${gpu_idx}_throttle_reasons"  "$throttle"
    done
}

# ------------------------------------------------------------------
# main loop
# ------------------------------------------------------------------

echo "[env_sampler $(now_utc)] starting — ns=$NS label='$POD_LABEL' container=$CONTAINER interval=${INTERVAL}s" >&2
echo "[env_sampler $(now_utc)] output=${OUT}" >&2

while true; do
    # Discover live training pods each cycle (handles pod restarts gracefully)
    mapfile -t PODS < <(kubectl get pods -n "$NS" -l "$POD_LABEL" \
        --field-selector=status.phase=Running \
        --no-headers -o custom-columns=':metadata.name' 2>/dev/null)

    if [ "${#PODS[@]}" -eq 0 ]; then
        warn "no Running pods found matching label '$POD_LABEL' in ns=$NS — will retry"
    else
        for pod in "${PODS[@]}"; do
            sample_pod "$pod" &
        done
        wait  # wait for all parallel pod samples to finish before next cycle
    fi

    sleep "$INTERVAL"
done
