#!/bin/bash
# Shared scratch of a durable endpoint call (docs/JOBS.md "Scratch storage", `call_scratch: shared`): the caller's /work is
# the tenant's shared-scratch claim at the API-derived leaf `calls/<operation>` (a kubelet subPath: the pod mounts the
# leaf only, never the tenant's directory). Three bounded operations on exactly that leaf, descriptor-safe, no symlink
# ever followed, nothing outside the mount ever touched:
#   provision <dir>            init container (root, CHOWN/FOWNER/DAC_OVERRIDE only): the kubelet creates a missing subPath
#                              directory root-owned 0755; make it the runner's (10001:10001, 2770) so main and the
#                              uploader can write; idempotent on a retry (existing files untouched)
#   proof <dir>                the durable capture is complete and intact: call.json says capture_complete, response.bin
#                              hashes to call.json's sha256, RESULT.json says export succeeded
#   cleanup <dir> <uid>        after `proof` and the API's acknowledgement only (upload.sh): remove the leaf's entries,
#                              every one owned by <uid>, symlinks removed as links (never followed); the leaf directory
#                              itself stays (the pod cannot unlink its own mount point: the retained empty leaf is the
#                              recorded GC gap); refused outright when <dir> is not a plain directory or any entry is
#                              someone else's
set -uo pipefail
die() { echo "scratch: $*" >&2; exit 1; }
plain_dir() { [ -d "$1" ] && [ ! -L "$1" ]; }
plain_file() { [ -f "$1" ] && [ ! -L "$1" ]; }

cmd="${1:-}"; dir="${2:-/work}"
case "$cmd" in
  provision)
    plain_dir "$dir" || die "provision: $dir is not a plain directory"
    chown -h 10001:10001 "$dir" || die "provision: chown of $dir failed"
    chmod 2770 "$dir" || die "provision: chmod of $dir failed"
    echo "scratch: provisioned $dir (10001:10001, 2770)";;
  proof)
    plain_dir "$dir" || die "proof: $dir is not a plain directory"
    plain_file "$dir/call.json" || die "proof: no call.json"
    plain_file "$dir/response.bin" || die "proof: no response.bin"
    [ "$(jq -r '.capture_complete // false' "$dir/call.json")" = true ] || die "proof: capture not complete"
    want=$(jq -r '.sha256 // empty' "$dir/call.json"); [ -n "$want" ] || die "proof: call.json carries no sha256"
    have=$(sha256sum "$dir/response.bin" | cut -d' ' -f1)
    [ "$have" = "$want" ] || die "proof: response.bin sha256 $have differs from the manifest's $want"
    plain_file "$dir/RESULT.json" || die "proof: no RESULT.json"
    [ "$(jq -r '.export // empty' "$dir/RESULT.json")" = succeeded ] || die "proof: RESULT.json does not say export succeeded"
    echo "scratch: proof ok ($want)";;
  cleanup)
    uid="${3:-10001}"
    plain_dir "$dir" || die "cleanup: $dir is not a plain directory; nothing removed"
    foreign=$(find "$dir" -mindepth 1 -xdev ! -user "$uid" 2>/dev/null | head -n 3)
    [ -z "$foreign" ] || die "cleanup refused: entries not owned by uid $uid under $dir: ${foreign//$'\n'/ }"
    # -P: never follow symlinks; -xdev: never cross into another filesystem; -delete removes a link as a link
    find -P "$dir" -mindepth 1 -xdev -delete || die "cleanup: removal under $dir failed"
    [ -z "$(find "$dir" -mindepth 1 | head -n 1)" ] || die "cleanup: $dir not empty afterwards"
    echo "scratch: leaf emptied ($dir); the directory itself is retained (gc gap recorded by the platform)";;
  *) die "usage: scratch.sh provision|proof|cleanup <dir> [uid]";;
esac
