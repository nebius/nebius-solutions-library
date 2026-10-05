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
#
# Real bug found and fixed (item: [GRAFANA-DOWN] watchdog
# follow-up): this used to treat ANY auth-enforced instance answering at
# GRAFANA_URL as "already up, nothing to do" -- a presence check, not an
# identity check. Confirmed live, this exact collision happened on a
# real cluster: a different, unrelated Grafana instance was already
# running on this host's port 3000 (GRAFANA_PORT's own default), so
# this check silently declared success against the WRONG instance,
# while this package's own real instance (on a different port) sat
# broken for two days with nothing noticing. GRAFANA_PORT itself was
# already correctly respected here (not the bug) -- the real gap was
# never verifying the thing found is genuinely THIS package's own.
#
# Fixed by a real identity check, not just presence: if this package's
# own credentials file already exists, authenticate against the found
# instance's real dashboard-by-UID endpoint for the real, fixed UID
# this package's own dashboard JSON ships with (GRAFANA_DASHBOARD_UID
# below -- a committed, stable value, not generated per-install). A
# real HTTP 200 there means the found instance both accepts OUR stored
# credentials AND has OUR dashboard provisioned -- the strongest
# ownership signal available without a dedicated identity endpoint
# (Grafana has none). Anything else (401/403 -- wrong credentials, a
# genuinely different instance; 404 -- right credentials but our
# dashboard isn't there, also not confirmed as ours) means this is NOT
# verifiably our instance.
#
# Honest limitation, not hidden: if no credentials file exists yet
# (e.g. a fresh clone of this repo on a host where something else
# already occupies the configured port, before this script has ever
# run here), there is nothing to authenticate with, so identity
# genuinely cannot be verified either way. Rather than silently
# trusting a presence-only match (today's original bug) or silently
# assuming the opposite, this is treated as unverifiable and reported
# as such -- the operator decides, not a guess either way.
GRAFANA_DASHBOARD_UID="straggler-detection-metrics"

info "Checking for a real, already-reachable Grafana instance at $GRAFANA_URL..."
_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$GRAFANA_URL/api/org" 2>/dev/null)"
if [ "$_code" = "401" ] || [ "$_code" = "302" ]; then
  if [ ! -f "$GRAFANA_ADMIN_CREDENTIALS_FILE" ]; then
    fail "A Grafana instance is already running at $GRAFANA_URL (HTTP $_code, auth enforced) but this is a fresh setup with no stored credentials yet ($GRAFANA_ADMIN_CREDENTIALS_FILE does not exist) -- cannot verify whether this is this package's own instance or a different, unrelated one already using this port. Set GRAFANA_PORT to a free port for this package's own instance, or if you know this IS this package's own instance from an earlier setup whose var/ directory was removed, restore $GRAFANA_ADMIN_CREDENTIALS_FILE first."
  else
    _probe_pw="$(grep -oP '(?<=^admin_password: ).*' "$GRAFANA_ADMIN_CREDENTIALS_FILE" 2>/dev/null)"
    _identity_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -u "admin:$_probe_pw" \
      "$GRAFANA_URL/api/dashboards/uid/$GRAFANA_DASHBOARD_UID" 2>/dev/null)"
    if [ "$_identity_code" = "200" ]; then
      info "Already up with real auth enforced AND confirmed as this package's own instance at $GRAFANA_URL (HTTP $_code; this package's own stored credentials + dashboard '$GRAFANA_DASHBOARD_UID' both confirmed live) -- nothing to do."
      exit 0
    else
      fail "A Grafana instance is running at $GRAFANA_URL (HTTP $_code, auth enforced) but isn't this package's own -- this package's own stored credentials did not confirm it (dashboard-by-UID check returned HTTP $_identity_code, expected 200). Set GRAFANA_PORT to use a different port for this package's own instance, or confirm this is intentional (e.g. a stale instance you want to replace -- stop it first, then re-run this script)."
    fi
  fi
elif [ "$_code" = "200" ]; then
  warn "Something is already answering at $GRAFANA_URL, but WITHOUT requiring credentials (HTTP 200 -- anonymous access enabled). Not managed by this script -- see README.md section 6.2 for why this matters and how to check/fix an existing instance's own config."
  exit 0
fi
[ "$FAIL" -eq 1 ] && { echo "[grafana-setup.sh] Aborting -- a Grafana instance occupies $GRAFANA_URL that isn't confirmed as this package's own -- see FATAL line(s) above." >&2; exit 1; }

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

# Real bug found live (this session): Grafana only discovers NEW
# provisioning PROVIDER config files (the dashboards/local.yaml and
# datasources/local.yaml files that tell it where to look at all) at
# its own process startup -- updateIntervalSeconds only governs an
# ALREADY-REGISTERED provider re-scanning its own configured path for
# dashboard-JSON content changes, not Grafana noticing a brand-new
# provider file that didn't exist yet when it booted. Previously this
# script left both provider files for install.sh to write later,
# meaning Grafana booted with ZERO registered providers and never
# picked up install.sh's real config without a manual restart --
# confirmed live: the dashboard never appeared until Grafana was
# restarted after install.sh had already run.
#
# Real fix: generate both provider files HERE, before Grafana's first
# start, using the same real sed substitutions install.sh's own Step
# 4.5 already does (so the two stay byte-for-byte consistent):
# - dashboards/local.yaml needs only $PKG_ROOT, which this script
#   already has -- fully correct from the very first boot, no guessing.
# - datasources/local.yaml needs the real VM_URL, which this script may
#   not know yet (vm-setup.sh is independent/order-agnostic). Uses the
#   same http://$(hostname):8428 convention vm-setup.sh itself
#   establishes as a real, live best guess -- if VM isn't reachable
#   there yet or install.sh later discovers a different real value, the
#   FILE already exists and is already being watched, so Grafana's own
#   periodic re-scan picks up install.sh's later correction with no
#   restart needed (this part of the original reasoning was correct;
#   only the "the file already exists at boot" precondition was missing).
sed "s|path: /root/P20g_pr_ready/dashboards_dropin|path: $PKG_ROOT/observability/dashboards|" \
  "$PKG_ROOT/observability/dashboards/provisioning/dashboards/local.yaml" \
  > "$GEN_PROV_DIR/dashboards/local.yaml"
_guess_vm_url="http://$(hostname):8428"
sed "s|url: http://worker-0:8428|url: $_guess_vm_url|" \
  "$PKG_ROOT/observability/dashboards/provisioning/datasources/local.yaml" \
  > "$GEN_PROV_DIR/datasources/local.yaml"
info "Wrote real dashboard + datasource provisioning config to $GEN_PROV_DIR before Grafana's first start (datasource VM_URL is a live best guess, $_guess_vm_url -- install.sh corrects this later if needed, picked up automatically since the file already exists)."

CUSTOM_INI="$GRAFANA_DIR/custom.ini"
cat > "$CUSTOM_INI" <<EOF
[server]
http_port = $GRAFANA_PORT
[paths]
data = $GRAFANA_DATA_DIR
provisioning = $GEN_PROV_DIR
EOF
cat "$GRAFANA_AUTH_CONFIG_FILE" >> "$CUSTOM_INI"
info "Wrote $CUSTOM_INI -- provisioning pointed directly at $GEN_PROV_DIR, already populated above."

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
