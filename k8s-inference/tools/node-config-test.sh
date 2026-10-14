#!/usr/bin/env bash
# charts/platform/files/node-config.sh (the containerd registry configuration of a node, docs/IMAGES.md): a fresh node
# gets the bootstrap mirror file, the memlock drop-in and ONE restart; a node that already serves the mirrors with
# unlimited memlock sees no write and no restart; a Spegel-written mirror file is never overwritten; a served runtime
# with a missing memlock drop-in is restarted once without touching the mirror file. Host commands are stubs; nothing
# leaves the temp directory. Run by tools/check.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
S="$ROOT/charts/platform/files/node-config.sh"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
B="$T/bin"; mkdir -p "$B"
cat > "$B/nsenter" <<'EOF'
#!/bin/bash
# the host's systemd/pidof through nsenter: run the part after `--` against the stubs (which record themselves)
while [ "$1" != "--" ]; do shift; done; shift
exec "$@"
EOF
cat > "$B/systemctl" <<'EOF'
#!/bin/bash
echo "systemctl $*" >> "$CALLS"; exit 0
EOF
cat > "$B/pidof" <<'EOF'
#!/bin/bash
echo 4242
EOF
cat > "$B/chroot" <<'EOF'
#!/bin/bash
# chroot <root> <cmd...>: run the command against the stubs
shift; exec "$@"
EOF
cat > "$B/crictl" <<'EOF'
#!/bin/bash
echo "crictl $*" >> "$CALLS"; [ "${PULL_OK:-1}" = 1 ]
EOF
cat > "$B/containerd" <<'EOF'
#!/bin/bash
echo "containerd $*" >> "$CALLS"; exit 0
EOF
# RACE=<file>: a `mkdir` that, after the real mkdir of the mirror directory, drops Spegel's peers file there — the file
# "appears" between the script's absence check and its publication
cat > "$B/mkdir" <<'EOF'
#!/bin/bash
/bin/mkdir "$@"
if [ -n "${RACE:-}" ] && [[ "${@: -1}" == */certs.d/registry.serverless2.local ]]; then
  printf 'server = "http://127.0.0.1:30500"\n\n[host."http://10.0.0.9:5000"]\n  capabilities = ["pull", "resolve"]\n' > "$RACE"
fi
EOF
chmod +x "$B"/*
node() { # <dir>: a host filesystem; memlock=<0|1> hosts=<none|spegel|boot> served=<0|1>
  local d=$1 memlock=$2 hosts=$3 limit=$4
  mkdir -p "$d/etc/containerd/certs.d" "$d/etc/systemd/system/containerd.service.d" "$d/proc/4242"
  printf 'version = 2\n[plugins."io.containerd.grpc.v1.cri".containerd]\n  snapshotter = "overlayfs"\n' > "$d/etc/containerd/config.toml"
  [ "$memlock" = 1 ] && printf '[Service]\nLimitMEMLOCK=infinity\n' > "$d/etc/systemd/system/containerd.service.d/serverless2-memlock.conf"
  if [ "$limit" = unlimited ]; then printf 'Max locked memory         unlimited            unlimited            bytes\n' > "$d/proc/4242/limits"; else printf 'Max locked memory         8388608              8388608              bytes\n' > "$d/proc/4242/limits"; fi
  case "$hosts" in
    spegel) mkdir -p "$d/etc/containerd/certs.d/registry.serverless2.local"; printf 'server = "http://127.0.0.1:30500"\n\n[host."http://10.0.0.7:5000"]\n  capabilities = ["pull", "resolve"]\n\n[host."http://127.0.0.1:30500"]\n  capabilities = ["pull", "resolve"]\n' > "$d/etc/containerd/certs.d/registry.serverless2.local/hosts.toml";;
    boot) mkdir -p "$d/etc/containerd/certs.d/registry.serverless2.local"; printf 'server = "http://127.0.0.1:30500"\n' > "$d/etc/containerd/certs.d/registry.serverless2.local/hosts.toml";;
  esac
  return 0
}
run() { # <dir> <pull ok> [race file] -> stdout; CALLS reset
  CALLS="$T/calls.txt"; : > "$CALLS"
  PATH="$B:$PATH" CALLS="$CALLS" PULL_OK="$2" RACE="${3:-}" HOST_ROOT="$1" IMAGES_HOST=registry.serverless2.local CACHE_NODE_PORT=30500 SETTLE_S=0 sh "$S"
}

# 1. a fresh CPU node (nothing written yet, memlock bounded, mirrors not served): bootstrap file + memlock + ONE restart
node "$T/fresh" 0 none bounded
out=$(run "$T/fresh" 0) || fail "fresh node: script failed: $out"
H="$T/fresh/etc/containerd/certs.d/registry.serverless2.local/hosts.toml"
[ -f "$H" ] || fail "fresh node: no bootstrap mirror file"
grep -q 'server = "http://127.0.0.1:30500"' "$H" || fail "fresh node: the mirror file does not point at the loopback cache"
[ -f "$T/fresh/etc/systemd/system/containerd.service.d/serverless2-memlock.conf" ] || fail "fresh node: no memlock drop-in"
[ -f "$T/fresh/etc/containerd/conf.d/serverless2-registry.toml" ] || fail "fresh node: no containerd drop-in"
grep -q '^imports = \["/etc/containerd/conf.d/\*.toml"\]' "$T/fresh/etc/containerd/config.toml" || fail "fresh node: imports line not added"
[ "$(grep -c 'systemctl restart containerd' "$T/calls.txt")" = 1 ] || fail "fresh node: expected exactly one containerd restart, got: $(cat "$T/calls.txt")"
grep -q 'systemctl daemon-reload' "$T/calls.txt" || fail "fresh node: no daemon-reload for the memlock drop-in"
[ -f "$T/fresh/etc/containerd/certs.d/.serverless2-containerd-restarted" ] || fail "fresh node: no restart marker"
echo "   ok fresh node: bootstrap mirror file, memlock drop-in, drop-in, one restart, marker"

# 2. an initialized node (Spegel wrote the mirror file with its peers, memlock unlimited, mirrors served): nothing written, no restart
node "$T/served" 1 spegel unlimited
before=$(sha256sum "$T/served/etc/containerd/certs.d/registry.serverless2.local/hosts.toml")
cfg_before=$(sha256sum "$T/served/etc/containerd/config.toml")
out=$(run "$T/served" 1) || fail "served node: script failed: $out"
[ "$(sha256sum "$T/served/etc/containerd/certs.d/registry.serverless2.local/hosts.toml")" = "$before" ] || fail "served node: Spegel's mirror file was rewritten"
[ "$(sha256sum "$T/served/etc/containerd/config.toml")" = "$cfg_before" ] || fail "served node: config.toml was touched"
[ ! -f "$T/served/etc/containerd/conf.d/serverless2-registry.toml" ] || fail "served node: a drop-in was written although the runtime serves the mirrors"
grep -q 'systemctl' "$T/calls.txt" && fail "served node: systemd was called: $(cat "$T/calls.txt")"
[ -f "$T/served/etc/containerd/certs.d/.serverless2-containerd-restarted" ] || fail "served node: the marker must be set once the probe passes"
echo "   ok served node: Spegel's mirror file untouched, no drop-in, no restart"

# 3. Spegel's file present, memlock unlimited, but the runtime does not serve the mirrors yet: drop-in + one restart, the mirror file untouched
node "$T/stale" 1 spegel unlimited
before=$(sha256sum "$T/stale/etc/containerd/certs.d/registry.serverless2.local/hosts.toml")
out=$(run "$T/stale" 0) || fail "stale runtime: script failed: $out"
[ "$(sha256sum "$T/stale/etc/containerd/certs.d/registry.serverless2.local/hosts.toml")" = "$before" ] || fail "stale runtime: Spegel's mirror file was rewritten"
[ "$(grep -c 'systemctl restart containerd' "$T/calls.txt")" = 1 ] || fail "stale runtime: expected one restart"
grep -q 'systemctl daemon-reload' "$T/calls.txt" && fail "stale runtime: daemon-reload without a memlock change"
echo "   ok stale runtime: one restart with the drop-in, Spegel's mirror file untouched, no daemon-reload"

# 4. a node booted with the cloud-init fragment (bootstrap file + memlock present, mirrors served): a no-op
node "$T/booted" 1 boot unlimited
before=$(sha256sum "$T/booted/etc/containerd/certs.d/registry.serverless2.local/hosts.toml")
out=$(run "$T/booted" 1) || fail "booted node: script failed: $out"
[ "$(sha256sum "$T/booted/etc/containerd/certs.d/registry.serverless2.local/hosts.toml")" = "$before" ] || fail "booted node: the boot-time mirror file was rewritten"
grep -q 'systemctl' "$T/calls.txt" && fail "booted node: systemd was called"
echo "   ok booted node (cloud-init fragment): no-op"
# 5. the race: Spegel writes its peers file between the absence check and the publication: Spegel's file wins
# byte-for-byte, no temporary is left behind, the rest of the run (memlock, served runtime) is unaffected
node "$T/race" 1 none unlimited
RH="$T/race/etc/containerd/certs.d/registry.serverless2.local/hosts.toml"
out=$(run "$T/race" 1 "$RH") || fail "race: script failed: $out"
grep -q '10.0.0.9:5000' "$RH" || fail "race: Spegel's peers file was clobbered by the bootstrap file"
[ "$(grep -c 'server = ' "$RH")" = 1 ] || fail "race: the mirror file is not Spegel's alone"
for leftover in "$T"/race/etc/containerd/certs.d/registry.serverless2.local/hosts.toml.bootstrap.*; do [ -e "$leftover" ] && fail "race: a temporary was left behind: $leftover"; done
echo "$out" | grep -q 'appeared meanwhile' || fail "race: the script did not report the lost race: $out"
grep -q 'systemctl' "$T/calls.txt" && fail "race: systemd was called on a served node"
echo "   ok race: Spegel's file published in between wins, no temporary left, no restart"
echo "node-config: all checks passed"
