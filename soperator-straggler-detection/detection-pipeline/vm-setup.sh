#!/bin/bash
# vm-setup.sh -- brings up a real, working VictoriaMetrics instance
# automatically, as a plain background process directly on this host
# (the Slurm control/login node), matching this project's own already-
# established precedent for Grafana (see grafana-standalone/README.md)
# rather than the srun-on-a-worker-node approach vm-standalone/README.md
# documents as the alternative (that approach remains valid -- run this
# script only if you want the login-node route).
#
# Run this BEFORE install.sh (either before or after environment.sh --
# the two are independent). Idempotent: a fast no-op if a real,
# reachable instance is already found at the real VM_URL this script
# would otherwise create.
#
# Never assumes network reachability from the worker nodes -- verifies
# it live, from a real worker node, before declaring success (see Step 4
# below). If this cluster's login node is firewalled off from its
# workers, this step fails loudly and precisely instead of silently
# leaving the aggregators unable to ever push real data.
set -u
PKG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAR_DIR="$PKG_ROOT/var"
VM_DIR="$PKG_ROOT/vm-standalone"
mkdir -p "$VAR_DIR"

FAIL=0
fail() { echo "FATAL: $*" >&2; FAIL=1; }
warn() { echo "WARNING: $*" >&2; }
info() { echo "[vm-setup.sh] $*"; }

apt_install() {
  if [ "$(id -u)" -eq 0 ]; then
    apt-get update -qq && apt-get install -y "$@"
  elif sudo -n true 2>/dev/null; then
    sudo apt-get update -qq && sudo apt-get install -y "$@"
  else
    echo "FATAL: not root and no passwordless sudo available -- cannot install: $*" >&2
    return 1
  fi
}

# Real, confirmed-live values (see VERSIONS.md's own confirmed
# `victoria-metrics-20260814-123346-tags-v1.150.0` value) -- pinned, not
# "latest", so this matches what the rest of this package was actually
# validated against. Real download URL + binary name + sha256 confirmed
# this session directly against VictoriaMetrics' own real GitHub release
# (not assumed):
#   curl -sL https://github.com/VictoriaMetrics/VictoriaMetrics/releases/download/v1.150.0/victoria-metrics-linux-amd64-v1.150.0_checksums.txt
VM_VERSION="v1.150.0"
VM_SHA256="22bfe77be3de1ad03f214a005129312536d77ed4e293b66c186df417ee40a61d"
VM_TARBALL="victoria-metrics-linux-amd64-${VM_VERSION}.tar.gz"
VM_URL_DOWNLOAD="https://github.com/VictoriaMetrics/VictoriaMetrics/releases/download/${VM_VERSION}/${VM_TARBALL}"
VM_BIN="${VM_BIN:-$VM_DIR/victoria-metrics-prod}"
VM_DATA_DIR="${VM_DATA_DIR:-$VM_DIR/data}"
# 8428 is VictoriaMetrics' own upstream default -httpListenAddr port --
# not a Soperator-specific convention -- reused unchanged only because
# this whole pipeline's other pieces (the Grafana datasource config,
# install.sh's own detection, every workload's default VM_URL) already
# assume it; override VM_PORT if you have a real reason to change it,
# but every one of those other pieces would then need updating too.
VM_PORT="${VM_PORT:-8428}"
VM_HOST="${VM_HOST:-$(hostname)}"
VM_URL="http://$VM_HOST:$VM_PORT"
LOG="$VAR_DIR/vm_supervised.log"

# =========================================================================
# Step 1 -- idempotent check: already up and reachable?
# =========================================================================

info "Checking for a real, already-reachable VictoriaMetrics instance at $VM_URL..."
if curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$VM_URL/health" 2>/dev/null | grep -q 200; then
  info "Already up and healthy at $VM_URL -- nothing to do. Pass VM_URL=$VM_URL to install.sh (or let it auto-detect this host)."
  exit 0
fi

# =========================================================================
# Step 2 -- fetch the real, pinned VictoriaMetrics binary if not already
# present (sha256-verified against VictoriaMetrics' own published
# checksum, not just trusted blindly).
# =========================================================================

mkdir -p "$VM_DIR"
if [ -x "$VM_BIN" ]; then
  info "Real VictoriaMetrics binary already present at $VM_BIN -- checking its own reported version..."
  real_version="$("$VM_BIN" --version 2>&1 | grep -oP 'tags-v\K[0-9]+\.[0-9]+\.[0-9]+' || true)"
  if [ "v$real_version" != "$VM_VERSION" ]; then
    warn "Existing binary at $VM_BIN reports version $real_version, not the pinned $VM_VERSION -- not overwriting automatically. Remove it first if you want this script to fetch the pinned version instead."
  fi
else
  info "Downloading real VictoriaMetrics $VM_VERSION from VictoriaMetrics' own GitHub release..."
  tmp_tarball="$(mktemp --suffix=.tar.gz)"
  if command -v curl >/dev/null 2>&1 && curl -fsSL "$VM_URL_DOWNLOAD" -o "$tmp_tarball"; then
    real_sha="$(sha256sum "$tmp_tarball" | awk '{print $1}')"
    if [ "$real_sha" != "$VM_SHA256" ]; then
      fail "Downloaded tarball's real sha256 ($real_sha) does not match VictoriaMetrics' own published checksum ($VM_SHA256) -- refusing to extract a corrupted/tampered download. Re-run, or verify manually against https://github.com/VictoriaMetrics/VictoriaMetrics/releases/download/${VM_VERSION}/${VM_TARBALL}_checksums.txt"
    else
      info "Real sha256 verified against VictoriaMetrics' own published checksum -- extracting..."
      tar -xzf "$tmp_tarball" -C "$VM_DIR" \
        && chmod +x "$VM_BIN" \
        && info "Real binary staged at $VM_BIN." \
        || fail "Extraction failed."
    fi
    rm -f "$tmp_tarball"
  else
    fail "Could not download $VM_URL_DOWNLOAD (or curl is missing) -- confirm this host can reach github.com, or stage the binary at $VM_BIN yourself (see vm-standalone/README.md)."
  fi
fi

[ "$FAIL" -eq 1 ] && { echo "[vm-setup.sh] Aborting -- see FATAL lines above." >&2; exit 1; }

# =========================================================================
# Step 3 -- launch via the same auto-restart supervisor pattern this
# project already uses for the aggregator/alert_engine.py (see
# observability/run_vm_supervised.sh), as a plain background process on
# THIS host -- not a Slurm allocation. Skips if a supervisor for this
# exact data dir is already running (same idempotency discipline as
# run.sh's own "already running? leave it alone" check).
# =========================================================================

_existing="$(pgrep -af "run_vm_supervised\.sh.*$VM_DATA_DIR" 2>/dev/null | head -1)"
if [ -n "$_existing" ]; then
  info "A supervisor for this exact data dir is already running (pid $(echo "$_existing" | awk '{print $1}')) -- leaving it alone."
else
  info "Launching victoria-metrics-prod under its own auto-restart supervisor (log: $LOG)..."
  setsid nohup bash "$PKG_ROOT/observability/run_vm_supervised.sh" "$VM_BIN" "$VM_DATA_DIR" ":$VM_PORT" "$LOG" \
    >/dev/null 2>&1 </dev/null &
  disown
fi

# =========================================================================
# Step 4 -- wait for real, live health, THEN verify real reachability
# from an actual worker node (not just localhost) -- this is the check
# that catches a firewalled-off login node before it becomes a much
# more confusing "aggregators never push data" mystery later.
# =========================================================================

info "Waiting for a real health-check pass (up to 60s)..."
_up=0
for _ in $(seq 1 20); do
  if curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$VM_URL/health" 2>/dev/null | grep -q 200; then
    _up=1
    break
  fi
  sleep 3
done
if [ "$_up" -ne 1 ]; then
  fail "victoria-metrics-prod did not become healthy within 60s -- check $LOG for the real error."
  echo "[vm-setup.sh] Aborting -- see FATAL lines above." >&2
  exit 1
fi
info "Real health check passed locally at $VM_URL."

_first_node="$(sinfo -N -h -o '%N' 2>/dev/null | sort -u | head -1)"
if [ -z "$_first_node" ]; then
  warn "Could not discover a real worker node via sinfo to verify cross-node reachability -- skipping that check. Confirm manually before trusting this instance: ssh <a worker node> \"curl -s $VM_URL/health\""
else
  info "Verifying real reachability from worker node $_first_node (not just localhost)..."
  _remote_check="$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$_first_node" \
    "curl -s -o /dev/null -w '%{http_code}' --max-time 5 '$VM_URL/health'" 2>/dev/null)"
  if [ "$_remote_check" = "200" ]; then
    info "Confirmed reachable from $_first_node -- this login node is NOT firewalled off from the workers for this pipeline's purposes."
  else
    fail "$VM_URL answered locally but did NOT answer from worker node $_first_node (got '$_remote_check', not 200) -- this login node is likely firewalled off from the workers on this cluster. The aggregators (which run on worker nodes) would never be able to push real data to this instance. Either open this port between login and worker nodes, or use the srun-on-a-worker-node approach instead (vm-standalone/README.md)."
  fi
fi

[ "$FAIL" -eq 1 ] && { echo "[vm-setup.sh] Aborting -- see FATAL lines above." >&2; exit 1; }

info "VictoriaMetrics ready at $VM_URL. install.sh will auto-detect this (its own hostname:8428 check) -- no VM_URL needed unless you override VM_HOST/VM_PORT above."
