#!/bin/sh
# node-config (charts/platform/templates/node-config, docs/IMAGES.md "The setting at boot"): run once per node by the
# DaemonSet's init container against the host's filesystem (HOST_ROOT, /host) with the host's systemd through nsenter.
# What it guarantees on a node, in this order, touching nothing that is already right:
#   1. the bootstrap mirror file for the logical host (IMAGES_HOST -> the Zot NodePort on the node's loopback) when
#      the node has NONE yet (a fresh node before Spegel ran): the first pulls of the logical host then go to the cache
#      instead of falling back to DNS ("server misbehaving"); a file that exists (cloud-init's, or Spegel's with its
#      peers) is NEVER rewritten here: Spegel owns it from its first run on;
#   2. the unlimited memlock on the containerd unit (a drop-in; a daemon-reload and one restart when it was missing);
#   3. containerd serving the per-registry mirror files: decided on the RUNNING process by a probe pull through the
#      logical host, never on a file; only when that fails is the drop-in written and containerd restarted once.
# A node that already serves the mirrors with unlimited memlock sees no write and no restart (the common case: every
# GPU node and every CPU batch node booted with the cloud-init fragment; every node this ran on before).
set -e
HOST_ROOT=${HOST_ROOT:-/host}
: "${IMAGES_HOST:?IMAGES_HOST is required}"
CACHE_NODE_PORT=${CACHE_NODE_PORT:-30500}
CFG=$HOST_ROOT/etc/containerd/config.toml
DROPIN=$HOST_ROOT/etc/containerd/conf.d/serverless2-registry.toml
CERTS=$HOST_ROOT/etc/containerd/certs.d
MARK=$CERTS/.serverless2-containerd-restarted
HOSTS=$CERTS/$IMAGES_HOST/hosts.toml
MEMLOCK=$HOST_ROOT/etc/systemd/system/containerd.service.d/serverless2-memlock.conf
PROBE=$IMAGES_HOST/docker/library/busybox:1.36
host_systemctl() { nsenter -t 1 -m -u -i -n -p -- systemctl "$@"; }
host_pid() { nsenter -t 1 -m -u -i -n -p -- pidof containerd | cut -d' ' -f1; }
probe_pull() { chroot "$HOST_ROOT" crictl pull "$PROBE" > /dev/null 2>&1; }

mkdir -p "$CERTS"
# 1. the bootstrap mirror file, only when absent, published WITHOUT clobbering: a hard link from a uniquely named
# temporary of this run (same filesystem) into the final name creates the file atomically and FAILS when a file exists
# there — Spegel may have written its peers file between the absence check and this publication; whatever exists wins,
# the temporary is removed. (A rename would silently replace it.)
if [ ! -f "$HOSTS" ]; then
  mkdir -p "$(dirname "$HOSTS")"
  TMP="$HOSTS.bootstrap.$$"
  printf 'server = "http://127.0.0.1:%s"\n\n[host."http://127.0.0.1:%s"]\n  capabilities = ["pull", "resolve"]\n' "$CACHE_NODE_PORT" "$CACHE_NODE_PORT" > "$TMP"
  if ln "$TMP" "$HOSTS" 2>/dev/null; then
    echo "bootstrap mirror file written for $IMAGES_HOST (the cache on 127.0.0.1:$CACHE_NODE_PORT; Spegel rewrites it with its peers)"
  else
    echo "mirror file for $IMAGES_HOST appeared meanwhile (Spegel): left as is"
  fi
  rm -f "$TMP"
else
  echo "mirror file for $IMAGES_HOST present: left as is"
fi
# 2. memlock (docs/JOBS.md "Multi-node runs"): RDMA memory registration is bounded by RLIMIT_MEMLOCK (8 MiB on the node
# image); an unlimited limit on the containerd unit lets every container register memory without CAP_IPC_LOCK
NEED_RESTART=0
if [ ! -f "$MEMLOCK" ]; then
  mkdir -p "$(dirname "$MEMLOCK")"
  printf '# serverless2 (docs/JOBS.md): RDMA pins memory; an unlimited memlock limit on the runtime lets every container\n# register memory without CAP_IPC_LOCK (the node image ships 8 MiB).\n[Service]\nLimitMEMLOCK=infinity\n' > "$MEMLOCK"
  host_systemctl daemon-reload
  NEED_RESTART=1
fi
cpid=$(host_pid || true)
if [ -n "$cpid" ] && ! grep -q 'locked memory.*unlimited' "$HOST_ROOT/proc/$cpid/limits" 2>/dev/null; then NEED_RESTART=1; fi
# 3. the running containerd serves the mirrors? (functional: a probe pull through the logical host, served by Spegel or
# the cache). Yes + memlock unlimited: nothing to do
if [ "$NEED_RESTART" = 0 ] && probe_pull; then
  [ -f "$MARK" ] || touch "$MARK"; echo "containerd serves the registry mirrors (probe pull ok) with unlimited memlock"; exit 0
fi
# Legacy repair: an earlier version appended the two tables to config.toml itself, which duplicates the cri.containerd
# table and makes the next containerd start fail. Drop that trailing block.
if [ "$(grep -c '^\[plugins."io.containerd.grpc.v1.cri".containerd\]$' "$CFG")" -gt 1 ]; then
  cp "$CFG" "$CFG.serverless2-backup"
  awk -v start="$(grep -n '^\[plugins."io.containerd.grpc.v1.cri".registry\]$' "$CFG" | tail -1 | cut -d: -f1)" 'NR < start' "$CFG" > "$CFG.new" && mv "$CFG.new" "$CFG"
  echo "removed the appended registry block from config.toml (backup: config.toml.serverless2-backup)"
fi
# a drop-in, never an edit of config.toml: its tables (cri.containerd) already exist there and TOML forbids defining a
# table twice. The imports line must precede the first table: after `version`.
mkdir -p "$HOST_ROOT/etc/containerd/conf.d"
grep -q '^imports' "$CFG" || sed -i '1a imports = ["/etc/containerd/conf.d/*.toml"]' "$CFG"
printf '# serverless2 image cache (docs/IMAGES.md): per-registry mirror configuration (Spegel peers, Zot cache)\nversion = 2\n[plugins."io.containerd.grpc.v1.cri".registry]\n  config_path = "/etc/containerd/certs.d"\n[plugins."io.containerd.grpc.v1.cri".containerd]\n  discard_unpacked_layers = false\n' > "$DROPIN"
chroot "$HOST_ROOT" containerd -c /etc/containerd/config.toml config dump > /dev/null   # parse check before a restart
host_systemctl restart containerd && touch "$MARK" && echo "containerd restarted with the registry configuration"
sleep "${SETTLE_S:-5}"; probe_pull && echo "verified: probe pull through the mirrors ok" || echo "configured; probe pull through $PROBE not yet possible (the cache or Spegel is not up yet: the next rollout re-checks)"
cpid=$(host_pid || true); [ -n "$cpid" ] && grep -q 'locked memory.*unlimited' "$HOST_ROOT/proc/$cpid/limits" 2>/dev/null && echo "verified: containerd memlock unlimited" || echo "!! containerd memlock limit still bounded (drop-in $MEMLOCK not applied?)"
