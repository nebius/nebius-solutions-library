#!/bin/bash
# Stage 2 install.sh -- brings a fresh cluster from nothing to a
# ready-to-run state, using only real, live discovery. Never assumes
# this project's own original 2-node/8-GPU-per-node development shape;
# every value below is queried from the real cluster this script is
# actually run against. Fails loudly and specifically on any genuinely
# missing prerequisite -- never silently proceeds with a guessed value.
#
# Does NOT build run.sh's job (Stage 3) -- this script only brings the
# environment up: discovers the real cluster shape, builds/installs
# what needs building/installing, and writes cluster.env for every
# other script in this package to read. It does not launch any
# workload or fault-injection test itself.
set -u
PKG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAR_DIR="$PKG_ROOT/var"
mkdir -p "$VAR_DIR"

FAIL=0
fail() { echo "FATAL: $*" >&2; FAIL=1; }
warn() { echo "WARNING: $*" >&2; }
info() { echo "[install.sh] $*"; }

# =========================================================================
# Step 1 -- real, live environment detection (no assumed values)
# =========================================================================

info "Discovering real cluster shape (never assuming the 2-node/8-GPU shape this package was built on)..."

command -v sinfo >/dev/null 2>&1 || fail "sinfo not found -- this script must run on a Slurm control/login node."
command -v scontrol >/dev/null 2>&1 || fail "scontrol not found -- this script must run on a Slurm control/login node."
command -v ssh >/dev/null 2>&1 || fail "ssh not found -- required to reach compute nodes."
command -v python3 >/dev/null 2>&1 || fail "python3 not found."
[ "$FAIL" -eq 1 ] && { echo "[install.sh] Aborting -- missing basic prerequisites, see FATAL lines above." >&2; exit 1; }

# Real node list: every node sinfo reports, deduplicated (a node can
# appear once per partition it belongs to -- confirmed live: this
# cluster's own worker-0/worker-1 both belong to 2 partitions each).
NODE_LIST="$(sinfo -N -h -o '%N' | sort -u | paste -sd, -)"
if [ -z "$NODE_LIST" ]; then
  fail "sinfo returned no nodes at all -- cannot discover cluster shape."
  exit 1
fi
NUM_NODES="$(echo "$NODE_LIST" | tr ',' '\n' | wc -l)"
info "Real discovered nodes ($NUM_NODES): $NODE_LIST"

# Real per-node GPU count, from scontrol's own live Gres field -- e.g.
# "gpu:nvidia_h200:8(S:0-1)" -> 8. Checked per-node, not assumed uniform:
# a real deployment could in principle mix node shapes, and this script
# should say so rather than silently averaging or picking one.
declare -A NODE_GPU_COUNTS
IFS=',' read -ra _NODES_ARR <<< "$NODE_LIST"
for node in "${_NODES_ARR[@]}"; do
  gres="$(scontrol show node "$node" -o | grep -oP 'Gres=\S+' | head -1)"
  gpu_count="$(echo "$gres" | grep -oP '(?<=:)\d+(?=\(|$)' | tail -1)"
  if [ -z "$gpu_count" ] || [ "$gpu_count" -eq 0 ] 2>/dev/null; then
    fail "could not determine a real GPU count for node $node (Gres='$gres')."
    continue
  fi
  NODE_GPU_COUNTS["$node"]="$gpu_count"
  info "  $node: $gpu_count real GPUs (Gres=$gres)"
done

GPUS_PER_NODE=""
for node in "${!NODE_GPU_COUNTS[@]}"; do
  c="${NODE_GPU_COUNTS[$node]}"
  if [ -z "$GPUS_PER_NODE" ]; then
    GPUS_PER_NODE="$c"
  elif [ "$c" != "$GPUS_PER_NODE" ]; then
    warn "node $node reports $c GPUs, but $GPUS_PER_NODE was seen on another node -- this fleet is NOT uniform. Using the minimum ($([ "$c" -lt "$GPUS_PER_NODE" ] && echo "$c" || echo "$GPUS_PER_NODE")) so no launch over-requests, but per-shape scripts may need manual review on the larger nodes."
    [ "$c" -lt "$GPUS_PER_NODE" ] && GPUS_PER_NODE="$c"
  fi
done
[ -z "$GPUS_PER_NODE" ] && { fail "could not determine any real GPU count."; exit 1; }
info "Using GPUS_PER_NODE=$GPUS_PER_NODE (real, live-discovered, minimum across the fleet)"

# =========================================================================
# NCCL / CUDA / compiler toolchain detection -- accounts for the real
# version-shadowing pattern this project's own history found: the
# container-bundled version, the Inspector-plugin build-tree version, and
# the version actually linked at runtime via the host bind-mount can all
# three differ. Report all three explicitly rather than assuming one.
# =========================================================================

info "Detecting real NCCL/CUDA/compiler state (per-node, not assumed uniform)..."

command -v nvcc >/dev/null 2>&1 || warn "nvcc not found on this control host -- fine if the Inspector plugin is built inside the training container instead (see below), but note this if you intended to build it here."
command -v gcc >/dev/null 2>&1 && command -v g++ >/dev/null 2>&1 || fail "gcc/g++ not found -- required to build the Inspector plugin and MoE's RDMA fault shim."

for node in "${_NODES_ARR[@]}"; do
  host_nccl="$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$node" \
    "find /usr/lib/x86_64-linux-gnu /usr/lib -maxdepth 1 -iname 'libnccl.so.*.*.*' 2>/dev/null | head -1" 2>/dev/null)"
  if [ -n "$host_nccl" ]; then
    info "  $node: host system NCCL library = $host_nccl"
  else
    warn "  $node: no host-installed libnccl.so.* found -- if this project's launch scripts' MOUNTS bind-mount /usr/lib/x86_64-linux-gnu into the training container (they do, by default), the container's own bundled NCCL will be used unshadowed there instead. Confirm this is the intended version for your workloads."
  fi
done

# =========================================================================
# /tmp tmpfs-vs-real-disk detection (the storage classifier's fault
# mechanism needs a real local-disk-backed /tmp bind-mounted into the
# container -- confirmed in this project's own history that the
# container base image's OWN /tmp is tmpfs). Checked on the HOST (what
# actually gets bind-mounted in), per node.
# =========================================================================

info "Detecting real /tmp filesystem type per node (storage classifier needs a real local-disk /tmp, not tmpfs)..."
for node in "${_NODES_ARR[@]}"; do
  fstype="$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$node" "findmnt -no FSTYPE /tmp 2>/dev/null || stat -f -c %T /tmp 2>/dev/null")"
  if echo "$fstype" | grep -qi "tmpfs"; then
    warn "  $node: host /tmp is tmpfs -- the storage-classifier's real disk-bound fault mechanism (storage-ebpf/real_disk_fault.py) will NOT produce genuine block-layer I/O here. Point workload launch scripts' MOUNTS at a real disk-backed path on this host instead of /tmp."
  else
    info "  $node: host /tmp filesystem = $fstype (real disk-backed, good -- this is what every workload's MOUNTS=...,/tmp:/tmp bind-mounts into the container)"
  fi
done

# =========================================================================
# bpftrace + tracefs detection and fix (per node) -- the real,
# already-established persistent fixes from this project's own history:
# the tracefs bind-mount wrapper, and (conditionally) a libLLVM SONAME
# conflict workaround. Probed live, never applied unconditionally.
# =========================================================================

info "Checking bpftrace/tracefs on every node (applying the established fix only where actually needed)..."
for node in "${_NODES_ARR[@]}"; do
  if ! ssh -o BatchMode=yes -o ConnectTimeout=10 "$node" "command -v bpftrace" >/dev/null 2>&1; then
    warn "  $node: bpftrace not installed. Installing (apt-get install -y bpftrace; confirmed working version elsewhere in this project's history: 0.20.2-1ubuntu4.3)."
    ssh -o BatchMode=yes "$node" "apt-get update -qq && apt-get install -y bpftrace" || fail "  $node: bpftrace install failed."
  fi

  tracefs_ok="$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$node" \
    "bpftrace -e 'tracepoint:syscalls:sys_enter_openat { exit(); }' >/dev/null 2>&1 && echo OK || echo FAIL")"
  if [ "$tracefs_ok" != "OK" ]; then
    info "  $node: bpftrace cannot attach a real tracepoint directly -- checking for the already-established tracefs bind-mount wrapper fix..."
    has_wrapper="$(ssh -o BatchMode=yes "$node" "test -f /usr/local/bin/bpftrace && grep -q sys-host /usr/local/bin/bpftrace 2>/dev/null && echo YES || echo NO")"
    if [ "$has_wrapper" = "NO" ]; then
      info "  $node: installing the tracefs bind-mount wrapper (storage-ebpf/bpftrace-tracefs-wrapper.sh)..."
      scp -o BatchMode=yes "$PKG_ROOT/storage-ebpf/bpftrace-tracefs-wrapper.sh" "$node:/tmp/_bpftrace_wrapper.sh" >/dev/null
      ssh -o BatchMode=yes "$node" '
        set -e
        if [ ! -f /usr/bin/bpftrace.real ]; then
          real_path="$(command -v bpftrace)"
          mv "$real_path" /usr/bin/bpftrace.real
        fi
        cp /tmp/_bpftrace_wrapper.sh /usr/local/bin/bpftrace
        chmod +x /usr/local/bin/bpftrace
      ' || fail "  $node: failed to install the tracefs bind-mount wrapper."
    else
      info "  $node: wrapper already installed."
    fi
  else
    info "  $node: bpftrace can already attach real tracepoints directly -- no wrapper needed."
  fi

  # Conditional libLLVM SONAME conflict check -- confirmed in this
  # project's own history to be needed on SOME nodes and not others;
  # probed here, never applied unconditionally.
  llvm_conflict="$(ssh -o BatchMode=yes "$node" \
    "ldd \$(readlink -f /usr/bin/bpftrace.real 2>/dev/null || command -v bpftrace) 2>/dev/null | grep -c 'not found'")"
  if [ "${llvm_conflict:-0}" -gt 0 ]; then
    warn "  $node: bpftrace's real binary has unresolved library dependencies (possible libLLVM SONAME conflict with the NVIDIA driver's own bundled LLVM) -- this needs manual investigation on this specific node before storage-classifier faults will produce real evidence there."
  fi
done

# =========================================================================
# libibverbs-dev detection (MoE's RDMA fault shim build dependency)
# =========================================================================

info "Checking for libibverbs-dev (needed to build workloads/moe/qp_rate_limit_shim.c)..."
for node in "${_NODES_ARR[@]}"; do
  if ! ssh -o BatchMode=yes "$node" "test -f /usr/include/infiniband/verbs.h" >/dev/null 2>&1; then
    info "  $node: libibverbs-dev not found -- installing..."
    ssh -o BatchMode=yes "$node" "apt-get update -qq && apt-get install -y libibverbs-dev" \
      || fail "  $node: libibverbs-dev install failed -- MoE's RDMA fault shim will not build there."
  else
    info "  $node: libibverbs-dev already present."
  fi
done

# =========================================================================
# logrotate detection (log-rotation setup)
# =========================================================================

if ! command -v logrotate >/dev/null 2>&1; then
  info "logrotate not found on this control host -- installing..."
  apt-get update -qq && apt-get install -y logrotate || warn "logrotate install failed -- alert_engine_supervised.log will grow unbounded (see observability/run_alert_engine_supervised.sh's own WARNING path, which already handles this gracefully at runtime)."
else
  info "logrotate already present."
fi

[ "$FAIL" -eq 1 ] && { echo "[install.sh] Aborting before build/setup steps -- see FATAL lines above." >&2; exit 1; }

# =========================================================================
# Step 2 -- build the Inspector plugin from source (NVIDIA upstream tree,
# confirmed patches already applied in the shipped source -- see
# INVENTORY.md's Inspector-plugin-provenance section). Built here, not
# shipped as a precompiled binary, so the plugin and whatever NCCL it
# links against always come from the exact same real build.
# =========================================================================

info "Building the Inspector plugin from source..."
if [ ! -d "$PKG_ROOT/../../nccl-2.28-src" ] && [ ! -d "/root/nccl-2.28-src" ]; then
  fail "No NCCL source tree with its own build/ found (expected e.g. /root/nccl-2.28-src/build/lib/libnccl.so) -- the Inspector plugin's Makefile needs NCCL_HOME pointing at one. This is a real prerequisite this script does not fetch itself (a full NCCL source checkout + build is a substantial, separate step) -- see inspector-plugin/README.md (shipped as UPSTREAM_README.md) for NVIDIA's own build instructions."
else
  NCCL_HOME="${NCCL_HOME:-/root/nccl-2.28-src/build}"
  CUDA_HOME="${CUDA_HOME:-$(dirname "$(dirname "$(command -v nvcc 2>/dev/null || echo /usr/local/cuda/bin/nvcc)")")}"
  info "Using NCCL_HOME=$NCCL_HOME CUDA_HOME=$CUDA_HOME"
  # Real, confirmed live: the Makefile's own `NCCL_HOME := ../../build`
  # uses `:=` (a plain assignment), which an environment variable of the
  # same name does NOT override in make's default mode -- it must be
  # passed as a command-line variable (`make VAR=value`) instead, which
  # DOES take precedence. Found live, this run, when the env-var form
  # silently built against the Makefile's own default path instead of
  # the real one detected above.
  ( cd "$PKG_ROOT/inspector-plugin" && make NCCL_HOME="$NCCL_HOME" CUDA_HOME="$CUDA_HOME" ) \
    && info "Inspector plugin built: $PKG_ROOT/inspector-plugin/libnccl-profiler-inspector.so" \
    || fail "Inspector plugin build failed."
  NCCL_LIB_PATH="$NCCL_HOME/lib"
fi
NCCL_LIB_PATH="${NCCL_LIB_PATH:-/root/nccl-2.28-src/build/lib}"

# =========================================================================
# Step 3 -- MoE's RDMA fault shim (reconstructed build command, see
# workloads/moe/README.md -- not independently verified there; verified
# for real here, now that libibverbs-dev is confirmed installed above).
# =========================================================================

info "Building MoE's RDMA fault shim (qp_rate_limit_shim.c)..."
( cd "$PKG_ROOT/workloads/moe" && gcc -shared -fPIC -o qp_rate_limit_shim.so qp_rate_limit_shim.c -ldl -libverbs ) \
  && info "qp_rate_limit_shim.so built successfully." \
  || warn "qp_rate_limit_shim.c build failed -- MoE's rank-fault (RDMA) variant will not work until this is resolved. Every other MoE fault variant is unaffected."

# =========================================================================
# Step 4 -- VictoriaMetrics: real, load-bearing config (0s dedup, long
# retention). Detect-and-reuse if a real, correctly-configured instance
# is already reachable (e.g. a prior install on this same cluster);
# otherwise this step needs a real binary + a node to run it on, which
# this script does not fetch/allocate itself (see vm-standalone/README.md
# for the exact real launch command and why each flag is load-bearing) --
# reported precisely rather than silently faked.
# =========================================================================

info "Checking for a real, reachable, correctly-configured VictoriaMetrics instance..."
VM_URL="${VM_URL:-}"
if [ -z "$VM_URL" ]; then
  # Try the conventional first-node:8428 this project's own history
  # always used, but only as a live PROBE, not an assumption -- if it
  # doesn't answer, we say so rather than silently defaulting to it.
  first_node="$(echo "$NODE_LIST" | tr ',' '\n' | sort | head -1)"
  candidate="http://$first_node:8428"
  if curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$candidate/health" 2>/dev/null | grep -q 200; then
    VM_URL="$candidate"
    info "Found a real, reachable VictoriaMetrics instance at $VM_URL (health check passed live)."
  fi
fi
if [ -z "$VM_URL" ]; then
  fail "No VM_URL given and no reachable instance found at the conventional http://<first-node>:8428. This script does not launch VictoriaMetrics itself (it needs a real binary staged on a node and a real Slurm allocation to run it on -- see vm-standalone/README.md for the exact launch command, the 0s-dedup/100y-retention rationale, and why each flag is load-bearing). Launch one per that README, then re-run install.sh with VM_URL=http://<host>:8428 set."
else
  info "Using VM_URL=$VM_URL"
fi

# =========================================================================
# Step 4.5 -- generate real, templated Grafana provisioning configs
# (STAGE2_HANDOFF.md item 5). The shipped datasources/local.yaml and
# dashboards/local.yaml are kept as static reference templates (this
# script doesn't know or assume where a real Grafana instance's own
# provisioning directory lives on the target cluster -- that's a real
# deployment decision, not something to guess at); this generates a
# real, ready-to-use copy with this cluster's actual discovered VM_URL
# and this package's actual real path substituted in, for the operator
# to point their own Grafana instance's provisioning config at (or copy
# into place), rather than hand-editing the hardcoded original values.
# =========================================================================

info "Generating real, templated Grafana provisioning configs..."
GEN_PROV_DIR="$VAR_DIR/grafana_provisioning_generated"
mkdir -p "$GEN_PROV_DIR/datasources" "$GEN_PROV_DIR/dashboards"
sed "s|url: http://worker-0:8428|url: $VM_URL|" \
  "$PKG_ROOT/observability/dashboards/provisioning/datasources/local.yaml" \
  > "$GEN_PROV_DIR/datasources/local.yaml"
sed "s|path: /root/P20g_pr_ready/dashboards_dropin|path: $PKG_ROOT/observability/dashboards|" \
  "$PKG_ROOT/observability/dashboards/provisioning/dashboards/local.yaml" \
  > "$GEN_PROV_DIR/dashboards/local.yaml"
info "Wrote real, templated Grafana provisioning configs to $GEN_PROV_DIR (point your Grafana instance's own provisioning directory at these, or copy them into place -- this script does not assume where that is)."

# =========================================================================
# Step 5 -- write cluster.env, the single source of real, discovered
# truth every other script in this package reads instead of hardcoding.
# =========================================================================

cat > "$PKG_ROOT/cluster.env" <<EOF
# Generated by install.sh at $(date -u +%FT%TZ) -- real, live discovery.
# Do not hand-edit; re-run install.sh instead.
NODE_LIST="$NODE_LIST"
NUM_NODES="$NUM_NODES"
GPUS_PER_NODE="$GPUS_PER_NODE"
NCCL_LIB_PATH="$NCCL_LIB_PATH"
VM_URL="$VM_URL"
EOF
info "Wrote $PKG_ROOT/cluster.env:"
sed 's/^/  /' "$PKG_ROOT/cluster.env"

if [ "$FAIL" -eq 1 ]; then
  echo "[install.sh] Completed WITH real, specific failures above -- this environment is NOT fully ready. See FATAL lines." >&2
  exit 1
fi
info "install.sh completed. Environment is ready (log rotation set up separately per-run by observability/run_alert_engine_supervised.sh, which now reads cluster.env for VM_URL)."
