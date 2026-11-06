#!/usr/bin/env bash
# services/jobs/bin/scratch.sh (the shared-scratch leaf of a durable endpoint call): proof and cleanup on exactly the
# owned leaf, descriptor-safe, symlinks removed as links and never followed, refusals for a symlinked or missing leaf,
# a foreign-owned entry, an incomplete or tampered capture, and an export that did not succeed. Run by tools/check.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
S="$ROOT/services/jobs/bin/scratch.sh"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
me=$(id -u)

capture() { # <leaf dir> [export]: a complete capture with a matching manifest and RESULT.json
  mkdir -p "$1"; printf 'hello' > "$1/response.bin"
  sha=$(sha256sum "$1/response.bin" | cut -d' ' -f1)
  printf '{"capture_complete": true, "sha256": "%s", "http_status": 200}' "$sha" > "$1/call.json"
  printf '{"export": "%s"}' "${2:-succeeded}" > "$1/RESULT.json"
  mkdir -p "$1/out"; printf 'x' > "$1/out/refresh.json"
}

# 1. proof: complete, intact, exported
capture "$T/leaf"; bash "$S" proof "$T/leaf" > /dev/null || fail "proof of a complete capture"
# 2. proof refusals: tampered bytes, incomplete capture, export failed, missing manifest, symlinked leaf
capture "$T/t"; printf 'tampered' > "$T/t/response.bin"; bash "$S" proof "$T/t" 2>/dev/null && fail "proof accepted a tampered capture"
capture "$T/i"; printf '{"capture_complete": false}' > "$T/i/call.json"; bash "$S" proof "$T/i" 2>/dev/null && fail "proof accepted an incomplete capture"
capture "$T/f" failed; bash "$S" proof "$T/f" 2>/dev/null && fail "proof accepted export=failed"
capture "$T/m"; rm "$T/m/call.json"; bash "$S" proof "$T/m" 2>/dev/null && fail "proof accepted a missing manifest"
capture "$T/real"; ln -s "$T/real" "$T/link"; bash "$S" proof "$T/link" 2>/dev/null && fail "proof accepted a symlinked leaf"
# 3. cleanup: the leaf's entries go, the leaf stays; a symlink inside is removed as a link, its target untouched
mkdir -p "$T/outside"; printf 'keep' > "$T/outside/precious"
capture "$T/c"; ln -s "$T/outside" "$T/c/escape"; ln -s "$T/outside/precious" "$T/c/escape-file"
bash "$S" cleanup "$T/c" "$me" > /dev/null || fail "cleanup of an owned leaf"
[ -d "$T/c" ] || fail "the leaf directory itself must stay"
[ -z "$(ls -A "$T/c")" ] || fail "the leaf is not empty: $(ls -A "$T/c")"
if [ ! -f "$T/outside/precious" ] || [ "$(cat "$T/outside/precious")" != keep ]; then fail "a symlink target was touched"; fi
[ -d "$T/outside" ] || fail "a symlinked directory target was removed"
# 4. cleanup refusals: a symlinked leaf, a missing leaf, a foreign-owned entry (another uid asserted), nothing removed
capture "$T/r"; ln -s "$T/r" "$T/rlink"; bash "$S" cleanup "$T/rlink" "$me" 2>/dev/null && fail "cleanup accepted a symlinked leaf"
[ -f "$T/r/response.bin" ] || fail "cleanup through a symlinked leaf removed something"
bash "$S" cleanup "$T/none" "$me" 2>/dev/null && fail "cleanup accepted a missing leaf"
capture "$T/o"; bash "$S" cleanup "$T/o" "$((me + 1))" 2>/dev/null && fail "cleanup accepted entries owned by another uid"
if [ ! -f "$T/o/response.bin" ] || [ ! -f "$T/o/call.json" ]; then fail "a refused cleanup removed entries"; fi
# 5. provision refuses a symlinked or missing leaf (the chown itself needs the init container's capabilities)
bash "$S" provision "$T/rlink" 2>/dev/null && fail "provision accepted a symlinked leaf"
bash "$S" provision "$T/none" 2>/dev/null && fail "provision accepted a missing leaf"
echo "   ok shared scratch: proof (complete/tampered/incomplete/failed/missing/symlinked), cleanup (leaf emptied, retained; symlinks as links; refusals), provision refusals"
