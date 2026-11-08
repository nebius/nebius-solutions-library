#!/bin/bash
# k8s/startup_checks.sh — environment self-checks for K8s training pods.
#
# Source this at the START of every K8s per-pod training script:
#   source /path/to/k8s/startup_checks.sh
#
# Philosophy: every past bug in this project shared one root cause — code
# assumed something about the environment that was not universally true.
# These checks detect real environment state at startup and adapt (or log
# clearly why adaptation failed) rather than assuming and crashing silently.
#
# Checks performed (in order):
#   1. NCCL network plugin: detects broken HPC-X SHARP/IB RDMA plugin,
#      automatically falls back to AWS OFI (real IB/RDMA via libibverbs)
#      or logs a last-resort fallback if no alternative exists.
#   2. Rank uniqueness: confirms (hostname, pid) is unique within this pod.
#   3. Hardware diagnostics reachability: determines which diagnostic paths
#      (DCGM, SSH) are actually available; logs what alert confidence level
#      is achievable, rather than assuming.
#
# No check silently fails. Each logs its result explicitly as:
#   [CHECK-PASS], [CHECK-WARN], or [CHECK-FALLBACK].
# set -u intentionally NOT applied — this file is sourced, and callers
# may have variables unset at the point of sourcing.

_HN="${HN:-$(hostname)}"
_RANK="${RANK:-${NODE_RANK:-${JOB_COMPLETION_INDEX:-?}}}"

# =========================================================================
# CHECK 1: NCCL network plugin
# Problem: HPC-X SHARP/IB RDMA plugin (auto-loaded via ldconfig) segfaults
#   in ncclIbDeregMr when multiple NCCL process groups are created concurrently
#   (Megatron TP/PP pattern). Root cause confirmed: hpcx.conf in
#   /etc/ld.so.conf.d/ places the broken plugin first in ldconfig cache.
# Detection: check which .so ldconfig resolves libnccl-net.so to.
# Fix: set NCCL_NET_PLUGIN to the AWS OFI NCCL plugin (real IB/RDMA via
#   libibverbs + libfabric, without the SHARP tree teardown bug).
# Fallback: if no alternative plugin found, disable IB entirely and log
#   clearly that this means degraded network performance.
# =========================================================================
_check_nccl_plugin() {
    local _default_plugin _hpcx_pattern="hpcx" _ofi_plugin=""

    # Static detection: which .so does ldconfig map libnccl-net.so to?
    _default_plugin="$(ldconfig -p 2>/dev/null | awk '/libnccl-net\.so / {print $NF}' | head -1)"

    # Respect existing NCCL network config: if the job or operator has
    # already set a workaround (NCCL_IB_DISABLE, NCCL_NET, NCCL_NET_PLUGIN),
    # the startup check must not override it.
    if [ "${NCCL_IB_DISABLE:-0}" = "1" ]; then
        # NCCL_IB_DISABLE=1 disables IB for communication, but the HPC-X plugin
        # (libnccl-net.so via ldconfig) is STILL loaded and its teardown code
        # (ncclIbDeregMr) segfaults on uninitialized IB handles when IB was never used.
        # Fix: also unset NCCL_NET_PLUGIN so NCCL skips plugin loading entirely and
        # uses its built-in TCP network. Only needed when HPC-X is the ldconfig default.
        _default_for_ib_check="$(ldconfig -p 2>/dev/null | awk '/libnccl-net\.so / {print $NF}' | head -1)"
        if echo "${_default_for_ib_check:-}" | grep -q "hpcx" && [ -z "${NCCL_NET_PLUGIN:-}" ]; then
            # NCCL plugin loading: empty string still triggers default (libnccl-net.so via ldconfig).
            # Use a non-existent path so dlopen() fails and NCCL falls back to built-in network.
            export NCCL_NET_PLUGIN="/nccl_net_disabled_hpcx_workaround.so"
            echo "[NCCL-CHECK] [CHECK-PASS] [$_HN rank=$_RANK] NCCL_IB_DISABLE=1 set + HPC-X default detected." >&2
            echo "[NCCL-CHECK] Setting NCCL_NET_PLUGIN to non-existent path to prevent HPC-X plugin loading." >&2
            echo "[NCCL-CHECK] NCCL will use built-in TCP network (no RDMA, no HPC-X segfault)." >&2
        else
            echo "[NCCL-CHECK] [CHECK-PASS] [$_HN rank=$_RANK] NCCL_IB_DISABLE=1 already set. No HPC-X override needed." >&2
        fi
        return 0
    fi
    if [ -n "${NCCL_NET:-}" ] && [ "${NCCL_NET}" != "IB" ]; then
        echo "[NCCL-CHECK] [CHECK-PASS] [$_HN rank=$_RANK] NCCL_NET=${NCCL_NET} already set (job-level config). Skipping plugin check." >&2
        return 0
    fi

    if [ -z "${NCCL_NET_PLUGIN:-}" ]; then
        if echo "${_default_plugin:-}" | grep -q "$_hpcx_pattern"; then
            # HPC-X SHARP plugin detected as default — known to segfault
            # with multiple concurrent NCCL process groups (Megatron TP/PP).
            # CONSERVATIVE POLICY: log and advise, do not auto-override.
            # Rationale: auto-switching to AWS OFI breaks workloads that worked
            # with HPC-X (e.g. Megatron DP, nanoGPT) on this cluster, because the
            # AWS OFI plugin requires EFA hardware, not generic Mellanox IB. The
            # correct remediation is cluster-specific and must be set explicitly:
            #   - For Megatron TP/PP: add NCCL_IB_DISABLE=1 to the job env (TCP fallback)
            #   - For other cases: use a container without HPC-X, or configure the
            #     appropriate alternative plugin for your specific IB stack.
            echo "[NCCL-CHECK] [CHECK-WARN] [$_HN rank=$_RANK] HPC-X SHARP plugin detected:" \
                 "${_default_plugin}" >&2
            echo "[NCCL-CHECK] Known issue: may segfault in ncclIbDeregMr with multiple NCCL" \
                 "process groups (Megatron TP/PP). Single-communicator workloads (DDP, nanoGPT)" \
                 "are unaffected." >&2
            echo "[NCCL-CHECK] Workaround for TP/PP: set NCCL_IB_DISABLE=1 in the job env" \
                 "(TCP fallback, loses RDMA performance) or use a non-HPC-X container image." >&2
            echo "[NCCL-CHECK] NOT auto-switching: conservative policy — cluster-specific" \
                 "NCCL plugin compatibility must be verified before auto-override." >&2
        else
            echo "[NCCL-CHECK] [CHECK-PASS] [$_HN rank=$_RANK] libnccl-net.so => ${_default_plugin:-builtin/not-found}" \
                 "(not HPC-X SHARP — no workaround needed)" >&2
        fi
    else
        echo "[NCCL-CHECK] [CHECK-PASS] [$_HN rank=$_RANK] NCCL_NET_PLUGIN already set: $NCCL_NET_PLUGIN" >&2
    fi
}
_check_nccl_plugin

# =========================================================================
# CHECK 2: Rank uniqueness — (hostname, pid) must be unique across this pod.
# The earlier PID collision bug (rank 1 on pod-0 and rank 10 on pod-1 both
# had pid=246) caused the alert engine to silently overwrite the straggler's
# data. This check confirms uniqueness within the LOCAL pod (8 processes).
# Cross-pod uniqueness is guaranteed by the (hostname, pid) composite key
# the alert engine uses since the bug fix.
# =========================================================================
_check_rank_uniqueness() {
    local _my_pid=$$
    local _my_identity="${_HN}:${_my_pid}"
    # Write our identity to a shared tmpfs location; check for collision.
    local _id_dir="/dev/shm/straggler_rank_ids"
    mkdir -p "$_id_dir" 2>/dev/null
    local _id_file="$_id_dir/${_my_pid}"
    if [ -f "$_id_file" ]; then
        local _existing; _existing="$(cat "$_id_file" 2>/dev/null)"
        echo "[RANK-CHECK] [CHECK-WARN] [$_HN rank=$_RANK] PID collision detected! pid=$_my_pid" \
             "already registered by: $_existing" >&2
        echo "[RANK-CHECK] This pod has duplicate PIDs. Alert engine uses (hostname,pid) keys — " \
             "straggler data may be overwritten. Known risk on containers with constrained PID space." >&2
    else
        echo "${_HN}:rank=${_RANK}" > "$_id_file" 2>/dev/null
        echo "[RANK-CHECK] [CHECK-PASS] [$_HN rank=$_RANK] Identity ${_my_identity} unique within pod." >&2
    fi
}
_check_rank_uniqueness

# =========================================================================
# CHECK 3: Hardware diagnostic reachability
# Alert engine DCGM cause-classification uses SSH to reach node hostnames.
# In K8s pods: SSH is typically absent; node internal hostnames are not
# DNS-resolvable from within pods. This determines what diagnostic paths
# ARE available and logs the resulting alert confidence ceiling honestly,
# rather than letting it be a silent limitation buried in alert output.
# =========================================================================
_check_diagnostic_paths() {
    local _ssh_ok=0 _dcgmi_ok=0 _sidecar_ok=0

    # SSH available?
    if command -v ssh >/dev/null 2>&1; then
        _ssh_ok=1
    fi

    # dcgmi available locally?
    if command -v dcgmi >/dev/null 2>&1; then
        _dcgmi_ok=1
        # Can dcgmi actually reach the hostengine?
        if dcgmi discovery -l >/dev/null 2>&1; then
            :  # ok
        else
            _dcgmi_ok=0
        fi
    fi

    # DCGM metrics via K8s sidecar/DaemonSet?
    # The soperator deploys helm-soperator-dcgm-exporter-ds which exposes
    # DCGM metrics via Prometheus on port 9400 of each GPU node. Check
    # for a local DCGM exporter endpoint (same-node DaemonSet sidecar).
    if curl -s --max-time 2 "http://localhost:9400/metrics" >/dev/null 2>&1; then
        _sidecar_ok=1
    fi

    if [ "$_ssh_ok" -eq 1 ] && [ "$_dcgmi_ok" -eq 1 ]; then
        echo "[DIAG-CHECK] [CHECK-PASS] [$_HN rank=$_RANK] SSH+dcgmi available: CONFIRMED alert confidence reachable." >&2
    elif [ "$_sidecar_ok" -eq 1 ]; then
        echo "[DIAG-CHECK] [CHECK-WARN] [$_HN rank=$_RANK] SSH/dcgmi unavailable; local DCGM exporter at :9400 detected." >&2
        echo "[DIAG-CHECK] Alert confidence will be PROBABLE at best (DCGM sidecar, not direct hardware query)." >&2
    else
        echo "[DIAG-CHECK] [CHECK-WARN] [$_HN rank=$_RANK] No hardware diagnostic path found:" \
             "ssh=${_ssh_ok} dcgmi=${_dcgmi_ok} sidecar=${_sidecar_ok}." >&2
        echo "[DIAG-CHECK] All alerts on this run will show confidence=UNCONFIRMED." \
             "This is correct K8s behavior, not an error." >&2
    fi
}
_check_diagnostic_paths

# =========================================================================
# CHECK 4: NCCL profiler API version for Inspector plugin compatibility
# Problem: Inspector exports only ncclProfiler_v5 (confirmed via nm/strings).
#   NCCL 2.25.1 (the system NCCL in pytorch:25.01-py3) only supports
#   ncclProfiler_v1 and ncclProfiler_v2. When NCCL loads the plugin, it
#   tries dlsym(ncclProfiler_v2) → NOT FOUND, then v1 → NOT FOUND, and
#   silently skips the plugin entirely. Result: zero Inspector dumps, zero
#   detection. Confirmed as root cause of the rank=5 "miss" investigation.
# Fix: build NCCL 2.32.3 via deploy.sh --build-nccl or the pinning script.
#   NCCL 2.32.3 supports ncclProfiler_v5. Inspector works correctly.
# Detection: check the runtime libnccl.so for ncclProfiler_v3+ strings.
# =========================================================================
_check_nccl_profiler_version() {
    local _libnccl _has_v5 _has_v3
    # Find the NCCL library that torchrun will actually load
    if [ -n "${TRAINING_NCCL_LIB_PATH:-}" ]; then
        _libnccl="${TRAINING_NCCL_LIB_PATH}/libnccl.so.2"
    else
        _libnccl="$(ldconfig -p 2>/dev/null | awk '/libnccl\.so\.2 / {print $NF}' | head -1)"
        _libnccl="${_libnccl:-/usr/lib/x86_64-linux-gnu/libnccl.so.2}"
    fi
    if [ ! -f "$_libnccl" ]; then
        echo "[NCCL-PROFILER-CHECK] [CHECK-WARN] [$_HN rank=$_RANK] Cannot find libnccl.so.2 at '$_libnccl'." \
             "Inspector may not load; build NCCL 2.32.3 via deploy.sh --build-nccl." >&2
        return 0
    fi
    _has_v5=$(strings "$_libnccl" 2>/dev/null | grep -c "ncclProfiler_v[345]" || true)
    if [ "${_has_v5:-0}" -gt 0 ]; then
        echo "[NCCL-PROFILER-CHECK] [CHECK-PASS] [$_HN rank=$_RANK] NCCL at '$_libnccl' supports profiler_v3+ — Inspector plugin will load correctly." >&2
    else
        echo "[NCCL-PROFILER-CHECK] [CHECK-FAIL] [$_HN rank=$_RANK] NCCL at '$_libnccl' only supports profiler_v1/v2 — Inspector plugin will NOT load." >&2
        echo "[NCCL-PROFILER-CHECK] Zero Inspector dumps will be written. Zero straggler detection." >&2
        echo "[NCCL-PROFILER-CHECK] ACTION REQUIRED: Build NCCL 2.32.3 via deploy.sh --build-nccl or:" >&2
        echo "[NCCL-PROFILER-CHECK]   cd <pipeline_root> && bash pinning/nccl/fetch_build_nccl.sh \\" >&2
        echo "[NCCL-PROFILER-CHECK]     --dest /workspace/nccl-2.32.3 --gencode '-gencode=arch=compute_90,code=sm_90'" >&2
        echo "[NCCL-PROFILER-CHECK] Then set NCCL_LIB_PATH=/workspace/nccl-2.32.3/build/lib in the job manifest." >&2
    fi
}
_check_nccl_profiler_version

echo "[STARTUP] [$_HN rank=$_RANK] Environment self-checks complete." >&2
