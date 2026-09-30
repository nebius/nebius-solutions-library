#!/bin/bash
# grafana-setup.sh -- brings up a real, auth-enforced Grafana instance
# automatically, as a plain background process directly on this host
# (the Slurm control/login node), matching this project's own already-
# established precedent (see grafana-standalone/README.md) and
# vm-setup.sh's own identical approach for VictoriaMetrics.
#
# Run this BEFORE install.sh (same independent slot as vm-setup.sh --
# order between the two doesn't matter, both just need to run before
# install.sh). Generates the real admin-password auth config ITSELF
# (mirroring install.sh's own Step 4.7 logic exactly -- same file, same
# idempotent "reuse if already present" rule) so install.sh finds and
# correctly reuses it afterward instead of generating a conflicting
# password -- no need to run install.sh twice.
#
# Also configures Grafana's own [paths] provisioning to point directly
# at var/grafana_provisioning_generated/ -- closing a gap install.sh's
# own Step 4.5 comment explicitly leaves as a manual step today. This is
# safe even though that directory doesn't have the real VM_URL in it
# yet at this point (install.sh hasn't run): the shipped
# dashboards/local.yaml provisioning config has its own
# updateIntervalSeconds=30, so Grafana re-scans and picks up install.sh's
# later, VM_URL-correct rewrite of that directory automatically -- no
# Grafana restart needed.
#
# Never assumes cluster-specific values -- real host, real paths, real
# live-verified auth posture throughout, same discipline as
# environment.sh/vm-setup.sh, so this works the same way on any real
# Soperator cluster, not just the one this was built against.
set -u
PKG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VAR_DIR="$PKG_ROOT/var"
GRAFANA_DIR="$PKG_ROOT/grafana-standalone"
mkdir -p "$VAR_DIR"

FAIL=0
fail() { echo "FATAL: $*" >&2; FAIL=1; }
warn() { echo "WARNING: $*" >&2; }
info() { echo "[grafana-setup.sh] $*"; }

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

# Real, confirmed-live values (see VERSIONS.md's own confirmed `11.5.1`)
# -- pinned, not "latest", so this matches what the rest of this package
# was actually validated against. Real download URL + sha256 confirmed
# this session directly against Grafana's own official release server
# (not assumed):
#   curl -sL https://dl.grafana.com/oss/release/grafana-11.5.1.linux-amd64.tar.gz.sha256
GRAFANA_VERSION="11.5.1"
GRAFANA_SHA256="a999fc4897b3d7dbbd1b8cba4a6fb7a6b37f9c2fd371804d2959839ed54e1746"
GRAFANA_TARBALL="grafana-${GRAFANA_VERSION}.linux-amd64.tar.gz"
GRAFANA_URL_DOWNLOAD="https://dl.grafana.com/oss/release/${GRAFANA_TARBALL}"
GRAFANA_HOMEPATH="${GRAFANA_HOMEPATH:-$GRAFANA_DIR/grafana-v${GRAFANA_VERSION}}"
GRAFANA_DATA_DIR="${GRAFANA_DATA_DIR:-$GRAFANA_DIR/data}"
# 3000 is Grafana's own upstream default http_port -- not a
# Soperator-specific convention -- reused unchanged only because
# install.sh's own detection (Step 4.6) already assumes it; override
# GRAFANA_PORT if you have a real reason to change it, but install.sh's
# own detection would then need updating too.
GRAFANA_PORT="${GRAFANA_PORT:-3000}"
GRAFANA_URL="http://localhost:$GRAFANA_PORT"
LOG="$VAR_DIR/grafana_supervised.log"
GEN_PROV_DIR="$VAR_DIR/grafana_provisioning_generated"
GRAFANA_ADMIN_CREDENTIALS_FILE="$VAR_DIR/grafana_admin_credentials.txt"
GRAFANA_AUTH_CONFIG_FILE="$VAR_DIR/grafana_auth_generated.ini"

# =========================================================================
# Step 1 -- idempotent check: already up with real auth enforced?
# =========================================================================

info "Checking for a real, already-reachable Grafana instance at $GRAFANA_URL..."
_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$GRAFANA_URL/api/org" 2>/dev/null)"
if [ "$_code" = "401" ] || [ "$_code" = "302" ]; then
  info "Already up with real auth enforced at $GRAFANA_URL (HTTP $_code) -- nothing to do."
  exit 0
elif [ "$_code" = "200" ]; then
  warn "Something is already answering at $GRAFANA_URL, but WITHOUT requiring credentials (HTTP 200 -- anonymous access enabled). Not managed by this script -- see README.md section 6.2 for why this matters and how to check/fix an existing instance's own config."
  exit 0
fi

# =========================================================================
# Step 2 -- fetch the real, pinned Grafana binary if not already present
# (sha256-verified against Grafana's own published checksum).
# =========================================================================

mkdir -p "$GRAFANA_DIR"
if [ -x "$GRAFANA_HOMEPATH/bin/grafana" ]; then
  info "Real Grafana binary already present at $GRAFANA_HOMEPATH/bin/grafana."
else
  info "Downloading real Grafana $GRAFANA_VERSION from Grafana's own official release server..."
  tmp_tarball="$(mktemp --suffix=.tar.gz)"
  if command -v curl >/dev/null 2>&1 && curl -fsSL "$GRAFANA_URL_DOWNLOAD" -o "$tmp_tarball"; then
    real_sha="$(sha256sum "$tmp_tarball" | awk '{print $1}')"
    if [ "$real_sha" != "$GRAFANA_SHA256" ]; then
      fail "Downloaded tarball's real sha256 ($real_sha) does not match Grafana's own published checksum ($GRAFANA_SHA256) -- refusing to extract a corrupted/tampered download."
    else
      info "Real sha256 verified against Grafana's own published checksum -- extracting..."
      # Real bug found live (this session): a bare `-C "$GRAFANA_DIR"`
      # extract lands at $GRAFANA_DIR/<whatever top-level dir name the
      # tarball itself uses> (grafana-vX.Y.Z/), which only happens to
      # equal $GRAFANA_HOMEPATH's own default by coincidence -- overriding
      # GRAFANA_HOMEPATH independently of GRAFANA_DIR then silently
      # extracts to the WRONG place, and the launch fails with a
      # confusing "no such file or directory" days later, not here.
      # Extract to a real, disposable staging dir instead, then place
      # whatever single top-level directory it contains at EXACTLY
      # $GRAFANA_HOMEPATH, regardless of the tarball's own internal
      # naming -- makes the override actually safe to use.
      _stage_dir="$(mktemp -d)"
      if tar -xzf "$tmp_tarball" -C "$_stage_dir"; then
        _extracted_dir="$(find "$_stage_dir" -mindepth 1 -maxdepth 1 -type d | head -1)"
        if [ -z "$_extracted_dir" ]; then
          fail "Extracted tarball but found no top-level directory inside it -- unexpected archive layout."
        else
          mkdir -p "$(dirname "$GRAFANA_HOMEPATH")"
          rm -rf "$GRAFANA_HOMEPATH"
          mv "$_extracted_dir" "$GRAFANA_HOMEPATH" \
            && info "Real binary staged at $GRAFANA_HOMEPATH." \
            || fail "Could not place the extracted Grafana release at $GRAFANA_HOMEPATH."
        fi
      else
        fail "Extraction failed."
      fi
      rm -rf "$_stage_dir"
    fi
    rm -f "$tmp_tarball"
  else
    fail "Could not download $GRAFANA_URL_DOWNLOAD (or curl is missing) -- confirm this host can reach dl.grafana.com, or stage a real Grafana release at $GRAFANA_HOMEPATH yourself (see grafana-standalone/README.md)."
  fi
fi

[ "$FAIL" -eq 1 ] && { echo "[grafana-setup.sh] Aborting -- see FATAL lines above." >&2; exit 1; }

# =========================================================================
# Step 3 -- real, enforced auth config -- mirrors install.sh's own Step
# 4.7 EXACTLY (same two files, same idempotent reuse rule) so
# install.sh correctly reuses this instead of generating a conflicting
# password when it runs afterward.
# =========================================================================

if [ -f "$GRAFANA_ADMIN_CREDENTIALS_FILE" ]; then
  _admin_pw="$(grep -oP '(?<=^admin_password: ).*' "$GRAFANA_ADMIN_CREDENTIALS_FILE" 2>/dev/null)"
  info "Reusing an already-generated real admin password from $GRAFANA_ADMIN_CREDENTIALS_FILE instead of invalidating it."
else
  _admin_pw="$(python3 -c "import secrets; print(secrets.token_urlsafe(18))")"
fi
cat > "$GRAFANA_ADMIN_CREDENTIALS_FILE" <<EOF
# Real, generated Grafana admin credentials -- never a hardcoded
# default. Restricted to this file's own owner (chmod 600).
# Retrieve with: cat $GRAFANA_ADMIN_CREDENTIALS_FILE
admin_user: admin
admin_password: $_admin_pw
EOF
chmod 600 "$GRAFANA_ADMIN_CREDENTIALS_FILE"
cat > "$GRAFANA_AUTH_CONFIG_FILE" <<EOF
# Generated by grafana-setup.sh (or install.sh) -- real, enforced auth:
# anonymous access explicitly disabled, a real random admin password.
# admin_user/admin_password here MUST match
# $GRAFANA_ADMIN_CREDENTIALS_FILE and only take effect on a genuinely
# fresh instance's FIRST start (Grafana ignores these once its own
# sqlite DB already exists).
[auth.anonymous]
enabled = false
[security]
admin_user = admin
admin_password = $_admin_pw
EOF
info "Wrote $GRAFANA_AUTH_CONFIG_FILE and $GRAFANA_ADMIN_CREDENTIALS_FILE (real, generated, never a hardcoded default)."

# =========================================================================
# Step 4 -- real custom.ini: base config + provisioning path (pointed
# directly at var/grafana_provisioning_generated/, closing the manual
# gap install.sh's own Step 4.5 comment currently leaves open) + the
# real auth fragment from Step 3 -- combined BEFORE first start, on a
# genuinely fresh data dir (auth only takes effect then).
# =========================================================================

mkdir -p "$GRAFANA_DATA_DIR" "$GEN_PROV_DIR/datasources" "$GEN_PROV_DIR/dashboards"
CUSTOM_INI="$GRAFANA_DIR/custom.ini"
cat > "$CUSTOM_INI" <<EOF
[server]
http_port = $GRAFANA_PORT
[paths]
data = $GRAFANA_DATA_DIR
provisioning = $GEN_PROV_DIR
EOF
cat "$GRAFANA_AUTH_CONFIG_FILE" >> "$CUSTOM_INI"
info "Wrote $CUSTOM_INI -- provisioning pointed directly at $GEN_PROV_DIR (install.sh's own later run keeps this directory's real VM_URL-correct contents up to date; Grafana's own updateIntervalSeconds re-scans it automatically, no restart needed)."

# =========================================================================
# Step 5 -- launch under the same auto-restart supervisor pattern this
# project already uses for the aggregator/alert_engine.py/VM (see
# observability/run_grafana_supervised.sh), as a plain background
# process on THIS host -- not a Slurm allocation. Skips if a supervisor
# for this exact data dir is already running.
# =========================================================================

_existing="$(pgrep -af "run_grafana_supervised\.sh.*$GRAFANA_DATA_DIR" 2>/dev/null | head -1)"
if [ -n "$_existing" ]; then
  info "A supervisor for this exact data dir is already running (pid $(echo "$_existing" | awk '{print $1}')) -- leaving it alone."
else
  info "Launching Grafana under its own auto-restart supervisor (log: $LOG)..."
  setsid nohup bash "$PKG_ROOT/observability/run_grafana_supervised.sh" "$GRAFANA_HOMEPATH" "$CUSTOM_INI" "$LOG" \
    >/dev/null 2>&1 </dev/null &
  disown
fi

# =========================================================================
# Step 6 -- wait for real, live health AND real enforced auth (401/302,
# NOT 200) before declaring success -- an honest check, not an assumption.
# =========================================================================

info "Waiting for real auth-enforced health (up to 60s)..."
_up=0
for _ in $(seq 1 20); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$GRAFANA_URL/api/org" 2>/dev/null)"
  if [ "$code" = "401" ] || [ "$code" = "302" ]; then
    _up=1
    break
  fi
  if [ "$code" = "200" ]; then
    fail "Grafana answered with HTTP 200 (anonymous access enabled) instead of 401/302 -- real auth is NOT enforced. This means $GRAFANA_DATA_DIR was not genuinely fresh when Grafana started (admin_user/admin_password only take effect on a data dir's first start -- see grafana-standalone/README.md). Remove $GRAFANA_DATA_DIR and re-run this script for real enforced auth from scratch, or use 'grafana-cli admin reset-admin-password' against the existing instance manually."
    break
  fi
  sleep 3
done
if [ "$_up" -ne 1 ] && [ "$FAIL" -ne 1 ]; then
  fail "Grafana did not become reachable within 60s -- check $LOG for the real error."
fi

[ "$FAIL" -eq 1 ] && { echo "[grafana-setup.sh] Aborting -- see FATAL lines above." >&2; exit 1; }

info "Real, auth-enforced Grafana ready at $GRAFANA_URL (HTTP $code confirmed, not 200). install.sh will detect and finalize this (real access host/instructions land in cluster.env's own GRAFANA_ACCESS_HOST after it runs -- see README.md section 6.1 for the exact SSH-tunnel command using that value). Credentials: cat $GRAFANA_ADMIN_CREDENTIALS_FILE"
