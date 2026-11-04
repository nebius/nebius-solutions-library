#!/bin/bash
# build.sh -- build the LD_PRELOAD pinning interposer (NEW, UNVALIDATED). Idempotent and concurrency-safe.
#   ./build.sh              always rebuild into this directory
#   ./build.sh --if-needed  build only if missing or older than thread_pin_new.c (this is what the launchers call)
# Prints the path of the usable .so on stdout (last line) and exits 0; exits 1 (printing nothing on stdout) if it cannot build.
# If this directory is read-only it builds into $PIN_BUILD_DIR (default /tmp/inspector-pin-<uid>).
# 8 ranks racing on one node are serialized with flock (or a mkdir lock if flock is absent).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/thread_pin_new.c"; NAME=libthread_pin_new.so
IF_NEEDED=0; [ "${1:-}" = "--if-needed" ] && IF_NEEDED=1
fresh() { [ -s "$1/$NAME" ] && [ "$1/$NAME" -nt "$SRC" ]; }
OUT="$HERE"; [ -w "$HERE" ] || OUT="${PIN_BUILD_DIR:-/tmp/inspector-pin-$(id -u)}"
mkdir -p "$OUT" 2>/dev/null
if [ $IF_NEEDED = 1 ]; then
  if fresh "$HERE"; then echo "$HERE/$NAME"; exit 0; fi
  if fresh "$OUT";  then echo "$OUT/$NAME";  exit 0; fi
fi
command -v gcc >/dev/null 2>&1 || { echo "[build.sh] gcc not found; cannot build $NAME (build it where gcc exists: ./build.sh)" >&2; exit 1; }
LOCK="$OUT/.build.lock"
do_build() {
  if [ $IF_NEEDED = 1 ] && fresh "$OUT"; then return 0; fi   # another rank built it while we waited
  gcc -O2 -Wall -shared -fPIC -o "$OUT/$NAME.tmp.$$" "$SRC" -ldl -lpthread >&2 || return 1
  mv -f "$OUT/$NAME.tmp.$$" "$OUT/$NAME"
}
if command -v flock >/dev/null 2>&1; then
  ( flock -w 120 9 || exit 1; do_build ) 9>"$LOCK" || { echo "[build.sh] build failed" >&2; exit 1; }
else
  n=0; until mkdir "$LOCK.d" 2>/dev/null; do sleep 1; n=$((n+1)); [ $n -gt 120 ] && { echo "[build.sh] lock timeout" >&2; exit 1; }; done
  do_build; rc=$?; rmdir "$LOCK.d" 2>/dev/null; [ $rc = 0 ] || { echo "[build.sh] build failed" >&2; exit 1; }
fi
echo "$OUT/$NAME"
