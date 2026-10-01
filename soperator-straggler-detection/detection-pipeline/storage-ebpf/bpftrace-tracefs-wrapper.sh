#!/bin/bash
# P20c persistent fix -- this jail's real host tracefs is visible at
# /sys-host/kernel/tracing, not bpftrace's expected standard path
# /sys/kernel/tracing (there's no live systemd/PID1 reachable from inside
# this jail to install a boot-time global bind-mount, so this wrapper
# applies the fix per-invocation instead, in a private mount namespace
# that doesn't touch global mount state -- safe to run concurrently).
# The libLLVM.so.18.1 dependency is handled separately and persistently
# via /etc/ld.so.conf.d/99-bpftrace-nvidia-llvm.conf + ldconfig, so no
# LD_LIBRARY_PATH override is needed here.
if mountpoint -q /sys/kernel/tracing 2>/dev/null; then
    exec /usr/bin/bpftrace.real "$@"
fi

# Real bug found live (a real customer cluster, this session): plain
# `unshare -m` can fail with "Operation not permitted" when the SSH/jail
# session this wrapper runs in isn't UID 0 and the jail's own user
# namespace doesn't grant CAP_SYS_ADMIN to a non-root user -- confirmed
# directly against the failing cluster. `unshare -m` (no `-U`) does NOT
# create a new user namespace, only a new mount namespace -- the process
# keeps its existing credentials/capabilities inside it, so if the
# unshare() syscall itself succeeds, the subsequent bind-mount always
# will too (same process, same capability set, just a new VFS view).
# That means the ONE real thing worth retrying under elevated privilege
# is the unshare call itself -- tried here via the SAME passwordless-
# sudo-or-fail discipline install.sh/environment.sh's own apt_install()
# already uses, not a new escalation mechanism. Plain (non-root) unshare
# is tried FIRST and unchanged from before on any host where it already
# works (confirmed on this project's own dev cluster) -- sudo is only
# ever invoked as a fallback, never assumed necessary.
BIND_AND_EXEC='mount --bind /sys-host/kernel/tracing /sys/kernel/tracing && exec /usr/bin/bpftrace.real "$@"'

if unshare -m true 2>/dev/null; then
    exec unshare -m bash -c "$BIND_AND_EXEC" -- "$@"
fi

if sudo -n unshare -m true 2>/dev/null; then
    exec sudo -n unshare -m bash -c "$BIND_AND_EXEC" -- "$@"
fi

echo "unshare: Operation not permitted, even via passwordless sudo -- this jail/container lacks CAP_SYS_ADMIN for unshare() entirely (not just for the current non-root user). Storage-fault detection (Path C) needs either that capability granted to this environment, or a persistent host-level tracefs bind-mount configured outside this project's own scope." >&2
exit 1
