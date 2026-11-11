#!/usr/bin/env python3
"""Capstone round 5 (2026-11) -- real fix for a genuine, newly-found gap in
dump-file space control: the existing dump_files.logrotate (size 300M,
copytruncate) cannot keep up with a long-running MoE job's real per-rank
dump-write rate. Found live: individual dump files reached 11-16GB within
~30 minutes of a real 48-GPU healthy soak test, and the logrotate process
itself got stuck holding its state-file lock mid-copy (copytruncate copies
the whole file before truncating; once the file is multi-GB and still
growing faster than the copy completes, the copy never finishes), which
silently blocks every subsequent rotation attempt ("state file is already
locked", confirmed live). A plain external truncate() was tried and
confirmed NOT to help either: the Inspector plugin holds one fd open for
its entire lifetime and keeps writing from ITS OWN kernel-tracked file
offset, completely independent of an unrelated process's truncate(2) call
on the same path -- the file was back to ~16GB of REAL allocated blocks
(confirmed via stat/du, not just apparent size) within under a minute of
being truncated to 0.

Real fix: fallocate(FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE) deallocates
real disk blocks for an arbitrary byte RANGE without changing the file's
apparent size or disturbing any other fd's write offset at all (confirmed
live on this filesystem: a 200M file written full of real data, punched
over its first 100M, dropped to 100M real blocks via `du` while `ls -la`
still reported the original 200M size unchanged). This is exactly the
right primitive for a file that is continuously appended to by a writer
we must not disturb: reclaim space for the byte range already consumed
(and therefore already durable in VictoriaMetrics -- same safety invariant
dump_files.logrotate's own "rotate 1" already relied on), leave everything
from the aggregator's last-checkpointed offset onward completely alone.

Reads the SAME .aggregator_offsets_checkpoint.json node_aggregator_ref.py
already maintains in this dump dir -- no new state, no coordination with
the aggregator process needed beyond reading a file it already writes.
Punching an already-punched range is a cheap no-op (confirmed via a
direct second-call test), so this re-punches the full known-safe range
every cycle rather than tracking per-file incremental state -- simpler,
and safe to run even if a cycle is skipped or duplicated.
"""
import json
import os
import sys
import time

MIN_RECLAIM_BYTES = 256 * 1024 * 1024   # don't bother for small files
BLOCK_FLOOR = 1024 * 1024               # floor the punch length to a 1MB boundary
SAFETY_MARGIN_BYTES = 8 * 1024 * 1024   # stay this far behind the checkpointed offset


def reclaim_once(dump_dir, log):
    ckpt_path = os.path.join(dump_dir, ".aggregator_offsets_checkpoint.json")
    try:
        with open(ckpt_path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return
    offsets = data.get("offsets", {})
    for path, offset in offsets.items():
        try:
            offset = int(offset)
        except (TypeError, ValueError):
            continue
        punch_len = offset - SAFETY_MARGIN_BYTES
        punch_len -= punch_len % BLOCK_FLOOR
        if punch_len < MIN_RECLAIM_BYTES:
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size < MIN_RECLAIM_BYTES:
            continue
        punch_len = min(punch_len, size)
        try:
            fd = os.open(path, os.O_RDWR)
        except OSError as e:
            print(f"[dump-reclaim] open failed for {path}: {e}", file=log, flush=True)
            continue
        try:
            # FALLOC_FL_PUNCH_HOLE=0x02, FALLOC_FL_KEEP_SIZE=0x01
            os.posix_fallocate  # noqa: B018 -- not used; punch-hole has no posix_fallocate equivalent
        except AttributeError:
            pass
        try:
            import ctypes
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            FALLOC_FL_KEEP_SIZE = 0x01
            FALLOC_FL_PUNCH_HOLE = 0x02
            ret = libc.fallocate(fd, FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE,
                                  ctypes.c_long(0), ctypes.c_long(punch_len))
            if ret != 0:
                errno = ctypes.get_errno()
                print(f"[dump-reclaim] fallocate failed for {path}: errno={errno}", file=log, flush=True)
            else:
                print(f"[dump-reclaim] punched [0,{punch_len}) of {path} "
                      f"(file size {size}, checkpoint offset {offset})", file=log, flush=True)
        finally:
            os.close(fd)


def main():
    if len(sys.argv) < 2:
        print("usage: dump_reclaim.py <dump_dir> [interval_s]", file=sys.stderr)
        sys.exit(1)
    dump_dir = sys.argv[1]
    interval_s = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
    while True:
        reclaim_once(dump_dir, sys.stdout)
        time.sleep(interval_s)


if __name__ == "__main__":
    main()
