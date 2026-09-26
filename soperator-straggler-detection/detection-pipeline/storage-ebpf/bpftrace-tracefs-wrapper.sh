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
else
    exec unshare -m bash -c 'mount --bind /sys-host/kernel/tracing /sys/kernel/tracing && exec /usr/bin/bpftrace.real "$@"' -- "$@"
fi
