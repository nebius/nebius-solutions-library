#!/bin/bash
# fetch_build_nccl.sh -- NEW, UNVALIDATED on clusters other than the one it was written on.
# Reproducible build of NCCL >= 2.32.3 (needed so the NCCL *profiler thread* can be pinned: ncclProfilerThreadCreate first
# appears in 2.32.x). A 255 MB binary does not belong in git, so the repo carries this script and a pinned tag instead.
#
#   ./fetch_build_nccl.sh [--check] [--dest DIR] [--tag v2.32.3-1] [--gencode "-gencode=arch=compute_90,code=sm_90"] [--jobs N]
#
#   --check  report whether DEST already holds a usable build (version + pinnable symbol) and exit 0/1; builds nothing.
# Defaults: tag v2.32.3-1; dest pinning/nccl/build-<tag> (git-ignored; put it on a SHARED filesystem so every node sees it).
# GPU arch: auto-detected from nvidia-smi compute_cap when a GPU is present (fast single-arch build); on a GPU-less login node
#   pass --gencode (or NVCC_GENCODE) matching the compute nodes, otherwise NCCL's full default arch set is built (slow, larger).
# On success it records the lib dir in pinning/.nccl_override_dir, which pin_env.sh picks up automatically (NCCL_OVERRIDE_DIR wins).
# Requires: git, make, g++, CUDA toolkit (nvcc; CUDA_HOME or /usr/local/cuda), network access to github.com.
# Honest caveat: the build is stripped of nothing on purpose -- do NOT strip libnccl.so; the pinning probe reads its .symtab.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; PIN="$(dirname "$HERE")"
TAG="${NCCL_TAG:-v2.32.3-1}"; DEST=""; GENCODE="${NVCC_GENCODE:-}"; JOBS="${JOBS:-$(nproc)}"; CHECK=0
while [ $# -gt 0 ]; do case "$1" in
  --check) CHECK=1;; --dest) DEST="$2"; shift;; --tag) TAG="$2"; shift;; --gencode) GENCODE="$2"; shift;; --jobs) JOBS="$2"; shift;;
  -h|--help) sed -n 2,16p "$0"; exit 0;; *) echo "unknown arg $1" >&2; exit 2;; esac; shift; done
DEST="${DEST:-$HERE/build-$TAG}"; LIB="$DEST/build/lib"
want_ver="${TAG#v}"; want_ver="${want_ver%%-*}"

verify() {  # prints a verdict, returns 0 only if version >= 2.32.3-class AND the profiler symbol is readable
  [ -e "$LIB/libnccl.so.2" ] || { echo "no libnccl.so.2 in $LIB"; return 1; }
  eval "$(NCCL_OVERRIDE_DIR="$LIB" PIN_MODE=full python3 -I "$PIN/pin_resolve.py")"
  echo "version=${PIN_NCCL_VERSION:-?} lib=${PIN_NCCL_LIB_RESOLVED:-?} -> resolved mode=$PIN_MODE_RESOLVED ($PIN_REASON)"
  [ "$PIN_MODE_RESOLVED" = "full" ] && [ "${PIN_NCCL_VERSION:-}" = "$want_ver" ]
}
if [ $CHECK = 1 ]; then verify; exit $?; fi
if verify >/dev/null 2>&1; then echo "[nccl] already built and verified at $LIB"; verify; echo "$LIB" > "$PIN/.nccl_override_dir"; exit 0; fi

for t in git make g++; do command -v $t >/dev/null || { echo "[nccl] missing required tool: $t" >&2; exit 1; }; done
export CUDA_HOME="${CUDA_HOME:-/usr/local/cuda}"
[ -x "$CUDA_HOME/bin/nvcc" ] || { echo "[nccl] nvcc not found under CUDA_HOME=$CUDA_HOME (set CUDA_HOME)" >&2; exit 1; }
if [ -z "$GENCODE" ] && command -v nvidia-smi >/dev/null 2>&1; then
  cc="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d '. ')"
  [ -n "$cc" ] && GENCODE="-gencode=arch=compute_${cc},code=sm_${cc}"
fi
[ -n "$GENCODE" ] && echo "[nccl] NVCC_GENCODE=$GENCODE" || echo "[nccl] no GPU/--gencode given: building NCCL's full default arch set (slow)"
SRC="$DEST/src"; mkdir -p "$DEST"
if [ ! -d "$SRC/.git" ]; then git clone --depth 1 --branch "$TAG" https://github.com/NVIDIA/nccl.git "$SRC" || { echo "[nccl] git clone failed" >&2; exit 1; }; fi
( cd "$SRC" && git describe --tags 2>/dev/null )
( cd "$SRC" && nice make -j"$JOBS" src.build CUDA_HOME="$CUDA_HOME" BUILDDIR="$DEST/build" ${GENCODE:+NVCC_GENCODE="$GENCODE"} ) || { echo "[nccl] build failed" >&2; exit 1; }
if verify; then echo "$LIB" > "$PIN/.nccl_override_dir"; echo "[nccl] OK: recorded $LIB in pinning/.nccl_override_dir"; exit 0; fi
echo "[nccl] build finished but verification failed (see message above)" >&2; exit 1
