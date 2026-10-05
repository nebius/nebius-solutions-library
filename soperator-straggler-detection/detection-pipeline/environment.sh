#!/bin/bash
# environment.sh -- brings a fresh host up to the ONE prerequisite tier
# install.sh does not attempt itself: a real C++ compiler toolchain, a
# matching CUDA toolkit, and a real NCCL source tree built at the exact
# path/version install.sh's own Step 2 already expects and otherwise
# fails loudly without (see install.sh's own FATAL message there:
# "No NCCL source tree with its own build/ found").
#
# Deliberately does NOT duplicate anything install.sh already handles
# itself (bpftrace, libibverbs-dev, logrotate, Slurm/ssh/python3
# detection, per-node GPU/NCCL/tmp discovery) -- see install.sh's own
# Step 1 for those. Run this BEFORE install.sh on a genuinely fresh
# host; if everything below is already present, this is a fast no-op.
#
# Runs on the SAME single host install.sh itself runs on (the Slurm
# control/login node) -- matching install.sh's own Step 2/3 execution
# model, where the Inspector plugin and MoE's RDMA shim are built once,
# locally, not per-compute-node (this project's own shared-storage
# convention makes the resulting build artifacts reachable from every
# node without a separate per-node build).
#
# Never touches the GPU driver: the CUDA install below is the
# `cuda-toolkit-13-0` package specifically, which NVIDIA's own apt repo
# ships independently of `cuda-drivers`/`cuda` (the metapackages that
# would touch the driver) -- confirmed via NVIDIA's own repo layout,
# not assumed.
set -u
PKG_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

FAIL=0
fail() { echo "FATAL: $*" >&2; FAIL=1; }
warn() { echo "WARNING: $*" >&2; }
info() { echo "[environment.sh] $*"; }

# Real bug found live (this session, on a genuinely non-root cluster):
# a bare `apt-get install` assumes root -- fails with "Permission
# denied" on /var/lib/apt/lists/lock for any non-root user, even one
# with passwordless sudo available, and the raw apt error output alone
# doesn't tell you that's the real cause. Runs as-is if already root,
# transparently prefixes `sudo` when passwordless sudo is available,
# and gives one precise, actionable message (not a raw apt error dump)
# if neither applies -- never silently retries as a different user.
apt_install() {
  if [ "$(id -u)" -eq 0 ]; then
    apt-get update -qq && apt-get install -y "$@"
  elif sudo -n true 2>/dev/null; then
    sudo apt-get update -qq && sudo apt-get install -y "$@"
  else
    echo "FATAL: not running as root and no passwordless sudo available -- cannot install: $* -- either re-run this script as root/with sudo, or install these packages yourself first: $*" >&2
    return 1
  fi
}

# Real, confirmed-live values this package was validated against (see
# VERSIONS.md) -- not arbitrary picks. NCCL_TAG independently confirmed
# to exist on NVIDIA's real upstream repo this session:
#   git ls-remote --tags https://github.com/NVIDIA/nccl.git | grep 2.28.9
#   -> refs/tags/v2.28.9-1
NCCL_TAG="v2.28.9-1"
NCCL_VERSION_DOTS="2.28.9"  # same pin, dotted form -- matches nccl.h's own NCCL_MAJOR/MINOR/PATCH, used for the apt-extracted (no .git) validity check below.
NCCL_APT_VERSION="2.28.9-1+cuda13.0"  # exact apt version string confirmed present in NVIDIA's own configured repo this session.
# Real, confirmed-live bug this project's own history hit (the
# environment.sh-recompilation investigation): a BARE default here only
# ever checked ONE of the two candidate locations install.sh itself
# accepts, so a perfectly valid, already-built tree sitting at the
# OTHER candidate was invisible to this script -- triggering a real,
# reproduced ~16-minute unnecessary rebuild at the wrong path while a
# complete build sat untouched two directories away. Fixed: when the
# caller hasn't set NCCL_SRC_DIR explicitly, BOTH of install.sh's own
# candidates are searched, same preference order install.sh itself
# uses (see nccl_tree_is_valid below) -- not just the first one.
# Setting NCCL_SRC_DIR explicitly still means exactly that one location
# and nothing else, same as before.
_NCCL_SRC_DIR_EXPLICIT="${NCCL_SRC_DIR:-}"
_NCCL_SRC_DIR_PKGROOT_SIBLING="$PKG_ROOT/../../nccl-2.28-src"
_NCCL_SRC_DIR_ROOT_FALLBACK="/root/nccl-2.28-src"
CUDA_PKG="cuda-toolkit-13-0"
CUDA_VERSION_WANT="13.0"

# =========================================================================
# Step 1 -- C++ compiler toolchain (gcc/g++/make) + git
# =========================================================================

info "Checking compiler toolchain (gcc/g++/make) + git..."
missing_tools=()
for t in gcc g++ make git; do
  command -v "$t" >/dev/null 2>&1 || missing_tools+=("$t")
done
if [ "${#missing_tools[@]}" -gt 0 ]; then
  info "Missing: ${missing_tools[*]} -- installing build-essential + git via apt..."
  apt_install build-essential git \
    || fail "Compiler toolchain install failed -- cannot proceed to the CUDA/NCCL steps below."
else
  info "gcc/g++/make/git already present."
fi

[ "$FAIL" -eq 1 ] && { echo "[environment.sh] Aborting -- see FATAL lines above." >&2; exit 1; }

# =========================================================================
# Step 2 -- CUDA toolkit (pinned to $CUDA_VERSION_WANT, the real value
# VERSIONS.md confirms this package was validated against). Installs
# ONLY the toolkit package -- never cuda-drivers/cuda-drivers-<ver> or
# the bare `cuda` metapackage, so an already-installed, already-working
# GPU driver is never touched.
# =========================================================================

info "Checking CUDA toolkit..."
real_nvcc_version=""
if command -v nvcc >/dev/null 2>&1; then
  real_nvcc_version="$(nvcc --version | grep -oP 'release \K[0-9]+\.[0-9]+')"
  info "Real, live nvcc found: release $real_nvcc_version"
fi

if [ "$real_nvcc_version" = "$CUDA_VERSION_WANT" ]; then
  info "CUDA toolkit $CUDA_VERSION_WANT already present -- matches VERSIONS.md, nothing to do."
elif [ -n "$real_nvcc_version" ]; then
  warn "nvcc found but reports release $real_nvcc_version, not the $CUDA_VERSION_WANT this package was validated against (see VERSIONS.md). NOT overwriting an existing toolkit automatically -- NVIDIA's apt repo supports multiple side-by-side toolkit versions; if the Inspector-plugin build in install.sh's Step 2 fails or behaves unexpectedly, install $CUDA_PKG alongside the existing one explicitly and point CUDA_HOME at it before re-running install.sh."
else
  info "No nvcc found -- installing $CUDA_PKG via NVIDIA's own official apt repo..."
  if [ -r /etc/os-release ]; then
    . /etc/os-release
  else
    fail "Cannot read /etc/os-release to determine the real distro for NVIDIA's apt-repo URL."
  fi
  if [ "$FAIL" -ne 1 ]; then
    distro_tag="${ID}${VERSION_ID//./}"   # e.g. ubuntu2204, ubuntu2404 -- NVIDIA's own real repo naming convention
    arch_tag="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
    [ "$arch_tag" = "amd64" ] && arch_tag="x86_64"
    keyring_url="https://developer.download.nvidia.com/compute/cuda/repos/${distro_tag}/${arch_tag}/cuda-keyring_1.1-1_all.deb"
    info "Real, live-detected distro tag: $distro_tag (from /etc/os-release: ID=$ID VERSION_ID=$VERSION_ID)"
    tmp_deb="$(mktemp --suffix=.deb)"
    if command -v curl >/dev/null 2>&1 && curl -fsSL "$keyring_url" -o "$tmp_deb"; then
      if [ "$(id -u)" -eq 0 ]; then
        dpkg -i "$tmp_deb"
      elif sudo -n true 2>/dev/null; then
        sudo dpkg -i "$tmp_deb"
      else
        fail "Not running as root and no passwordless sudo available -- cannot install the CUDA apt-repo keyring ($tmp_deb). Either re-run as root/with sudo, or run 'dpkg -i $tmp_deb' yourself first."
      fi
      [ "$FAIL" -ne 1 ] && { apt_install "$CUDA_PKG" \
        || fail "CUDA toolkit install failed after installing the real keyring -- check $keyring_url is reachable and $CUDA_PKG exists for $distro_tag."; }
      rm -f "$tmp_deb"
    else
      fail "Could not reach NVIDIA's own CUDA apt-repo keyring at $keyring_url for real, live-detected distro '$distro_tag' (or curl is missing) -- confirm this host's real distro/arch and NVIDIA's current repo layout at https://developer.download.nvidia.com/compute/cuda/repos/, then install $CUDA_PKG manually."
    fi
  fi
fi

[ "$FAIL" -eq 1 ] && { echo "[environment.sh] Aborting -- see FATAL lines above." >&2; exit 1; }

# =========================================================================
# Step 3 -- NCCL source tree, built at the exact path install.sh's own
# Step 2 expects (NCCL_HOME=$NCCL_SRC_DIR/build). Pinned to the real
# upstream tag confirmed present in this project's own history
# (INVENTORY.md's Inspector-plugin-provenance section: real NVIDIA
# upstream, `origin https://github.com/NVIDIA/nccl.git`, "NCCL 2.28.9-1"
# in its own git log). This ONLY builds the NCCL_HOME the Inspector
# plugin compiles its headers against -- the NCCL version actually
# linked at runtime for real training is a SEPARATE, host-shadowed
# concern install.sh's own NCCL note already documents (see README.md's
# Requirements section) and this script does not touch.
#
# Real investigation finding (confirmed, not assumed): the Inspector
# plugin's own Makefile never actually includes anything from
# NCCL_HOME/include at build time (zero #include of nccl.h/nccl_device*
# anywhere in inspector-plugin/ -- confirmed by grep AND by a real build
# that succeeded with NCCL_HOME pointed at a nonexistent path; its own
# profiler-API headers are a permanently vendored copy under
# inspector-plugin/nccl/, not read from NCCL_HOME). What DOES genuinely
# need a real, exact-version, COMPILED libnccl.so is the separate
# NCCL_LIB_PATH/LD_LIBRARY_PATH runtime-pinning mechanism several
# workload launch scripts use (see their own NCCL_LIB_PATH comments) --
# that's the real reason this step still produces a full build, not a
# headers-only tree.
# =========================================================================

# Real, disclosed failure mode this project's own history has hit live:
# a plain `git clone` failure is very often a LOCAL permission problem
# (can't create the target's parent, e.g. a non-root user against
# /root/nccl-2.28-src), not a network/DNS issue -- checked explicitly so
# the real cause is reported, not a misleading "confirm this host can
# reach github.com" guess.
_nccl_check_writable_parent() {
  local parent_dir
  parent_dir="$(dirname "$1")"
  if [ ! -w "$parent_dir" ]; then
    fail "Cannot write to $parent_dir (real permission check, not a guess) -- needs write access to its parent directory. If you're not running as root/with sudo and this repo is checked out under /root, either re-run with sudo, or set NCCL_SRC_DIR to a location you can write to (e.g. NCCL_SRC_DIR=\$HOME/nccl-2.28-src ./environment.sh) -- install.sh checks the PKG_ROOT-relative sibling location first and falls back to /root/nccl-2.28-src, so anything else needs NCCL_HOME/NCCL_SRC_DIR set explicitly for both scripts to agree on where it is."
    return 1
  fi
  return 0
}

# "Compatible" means more than "the directory exists" -- a partial clone
# (confirmed real: this project's own investigation produced exactly
# this kind of debris, a .git present with no build/ yet) must NOT be
# mistaken for a valid build.
#
# Real refinement found DURING this fix's own validation, not assumed
# correct from the design alone: an earlier version of this check chose
# between a git-tag check and a header-version check based on whether
# $dir/.git existed, on the assumption that was a reliable proxy for
# "how did this candidate's build/ get here." It isn't -- confirmed
# live by deliberately reproducing a directory that had BOTH a stale
# .git (checked out at the WRONG tag, left over from an earlier failed
# attempt) AND a freshly apt-extracted, genuinely-correct build/: the
# .git-presence branch incorrectly trusted the stale tag over the real
# build output and rejected a valid result (safe-direction only --  it
# fell through to a slower rebuild, never to silently trusting
# something invalid -- but still real, avoidable waste). Fixed by
# checking the one signal that's authoritative either way: a real git-
# clone+build run and the apt-extracted package both produce a real
# build/include/nccl.h with the same NCCL_MAJOR/MINOR/PATCH #defines
# (confirmed directly against both this cluster's own git-built tree
# and the extracted apt package) -- so this is checked unconditionally,
# with no branching on .git at all.
nccl_tree_is_valid() {
  local dir="$1"
  [ -f "$dir/build/lib/libnccl.so" ] || return 1
  [ -d "$dir/build/include" ] || return 1
  [ -f "$dir/build/include/nccl.h" ] || return 1
  local maj min pat
  maj="$(grep -oP '^#define NCCL_MAJOR \K[0-9]+' "$dir/build/include/nccl.h" 2>/dev/null)"
  min="$(grep -oP '^#define NCCL_MINOR \K[0-9]+' "$dir/build/include/nccl.h" 2>/dev/null)"
  pat="$(grep -oP '^#define NCCL_PATCH \K[0-9]+' "$dir/build/include/nccl.h" 2>/dev/null)"
  [ "$maj.$min.$pat" = "$NCCL_VERSION_DOTS" ]
}

# Faster alternative to a full git-clone+compile (confirmed live this
# session: ~37s download+extract vs. ~16 minutes for a real from-scratch
# `make -j src.build`): downloads NVIDIA's own prebuilt packages at the
# exact pinned version and extracts them directly -- NEVER `apt install`,
# so the host's own currently-installed libnccl2 (whatever version that
# is) is never touched or downgraded, and nothing is registered in
# dpkg's own database (confirmed live: `apt-get download` + `dpkg -x`
# left zero dpkg-database entries, checked both on a clean extraction
# and deliberately after a PARTIAL one). Any failure at any step --
# package not found, download failure, a corrupt/partial .deb, or the
# extracted version somehow not matching -- cleans up fully and returns
# non-zero so the caller falls through to the existing git-clone+build
# path; this function never leaves partial state behind for a later
# validity check to mistake as real (nccl_tree_is_valid would reject a
# truly partial extraction anyway, since both branches below are kept
# together or not written into target_dir/build/ at all).
try_nccl_apt_extraction() {
  local target_dir="$1"
  local tmp_dl
  tmp_dl="$(mktemp -d)"
  info "Trying apt-extraction of NCCL $NCCL_TAG (libnccl2/libnccl-dev=$NCCL_APT_VERSION) -- download+extract only, never apt install, never touches this host's own installed NCCL package..."
  if ! ( cd "$tmp_dl" && apt-get download "libnccl2=$NCCL_APT_VERSION" "libnccl-dev=$NCCL_APT_VERSION" >/dev/null 2>&1 ); then
    warn "apt-extraction: download failed (package unavailable at this exact version, or a network/repo issue) -- falling back to git-clone+build."
    rm -rf "$tmp_dl"
    return 1
  fi
  local deb
  for deb in "$tmp_dl"/*.deb; do
    if [ ! -f "$deb" ] || ! dpkg -x "$deb" "$tmp_dl/extracted" >/dev/null 2>&1; then
      warn "apt-extraction: extracting $deb failed (corrupt/partial download, or an unexpected package layout) -- cleaning up and falling back to git-clone+build."
      rm -rf "$tmp_dl"
      return 1
    fi
  done
  if [ ! -f "$tmp_dl/extracted/usr/include/nccl.h" ] || \
     [ ! -f "$tmp_dl/extracted/usr/lib/x86_64-linux-gnu/libnccl.so" ]; then
    warn "apt-extraction: extracted package is missing expected files (unexpected layout for this version) -- cleaning up and falling back to git-clone+build."
    rm -rf "$tmp_dl"
    return 1
  fi
  mkdir -p "$target_dir/build/include" "$target_dir/build/lib"
  cp -a "$tmp_dl/extracted/usr/include/." "$target_dir/build/include/"
  cp -a "$tmp_dl/extracted/usr/lib/x86_64-linux-gnu/." "$target_dir/build/lib/"
  rm -rf "$tmp_dl"
  if nccl_tree_is_valid "$target_dir"; then
    return 0
  fi
  warn "apt-extraction: post-extraction validity check failed unexpectedly (extracted version did not match $NCCL_VERSION_DOTS) -- cleaning up and falling back to git-clone+build."
  rm -rf "$target_dir/build"
  return 1
}

if [ -n "$_NCCL_SRC_DIR_EXPLICIT" ]; then
  _nccl_candidates=("$_NCCL_SRC_DIR_EXPLICIT")
else
  # Same two candidates, same preference order, install.sh itself
  # already checks -- this is the actual fix for the real, reproduced
  # bug (a valid build at the second candidate being invisible to this
  # script because it only ever checked the first).
  _nccl_candidates=("$_NCCL_SRC_DIR_PKGROOT_SIBLING" "$_NCCL_SRC_DIR_ROOT_FALLBACK")
fi

info "Checking for a real, valid NCCL $NCCL_TAG build among: ${_nccl_candidates[*]}..."
NCCL_SRC_DIR=""
for _c in "${_nccl_candidates[@]}"; do
  if nccl_tree_is_valid "$_c"; then
    NCCL_SRC_DIR="$_c"
    info "Real, valid NCCL $NCCL_TAG build already present at $_c/build -- nothing to do."
    break
  fi
done

if [ -z "$NCCL_SRC_DIR" ]; then
  NCCL_SRC_DIR="${_nccl_candidates[0]}"
  info "No valid NCCL $NCCL_TAG build found -- will obtain one at $NCCL_SRC_DIR (same first-candidate preference install.sh itself uses)."

  if try_nccl_apt_extraction "$NCCL_SRC_DIR"; then
    info "NCCL $NCCL_TAG installed via apt-extraction at $NCCL_SRC_DIR/build -- skipped the slower git-clone+compile path."
  else
    if [ ! -d "$NCCL_SRC_DIR/.git" ]; then
      if _nccl_check_writable_parent "$NCCL_SRC_DIR"; then
        info "Cloning real NVIDIA upstream NCCL source (https://github.com/NVIDIA/nccl.git) to $NCCL_SRC_DIR..."
        git clone https://github.com/NVIDIA/nccl.git "$NCCL_SRC_DIR" \
          || fail "NCCL clone failed for a reason other than local write permission (already checked OK) -- confirm this host can reach github.com (git ls-remote https://github.com/NVIDIA/nccl.git) and see git's own error output above."
      fi
    fi

    if [ "$FAIL" -ne 1 ]; then
      ( cd "$NCCL_SRC_DIR" && git fetch --tags && git checkout "$NCCL_TAG" ) \
        || fail "Could not check out real upstream tag $NCCL_TAG in $NCCL_SRC_DIR -- confirm this tag still exists (git ls-remote --tags https://github.com/NVIDIA/nccl.git)."
    fi

    if [ "$FAIL" -ne 1 ]; then
      real_cuda_home="${CUDA_HOME:-$(dirname "$(dirname "$(command -v nvcc 2>/dev/null || echo /usr/local/cuda/bin/nvcc)")")}"
      info "Building real NCCL $NCCL_TAG (make src.build, CUDA_HOME=$real_cuda_home -- NCCL's own standard documented build target, not invented here) -- this takes several real minutes (confirmed live: ~16 minutes on a 16-core host)..."
      ( cd "$NCCL_SRC_DIR" && make -j"$(nproc)" src.build CUDA_HOME="$real_cuda_home" ) \
        || fail "NCCL build failed -- see make's own output above for the real error."
    fi
  fi
fi

[ "$FAIL" -eq 1 ] && { echo "[environment.sh] Aborting -- see FATAL lines above." >&2; exit 1; }

info "Environment ready. You can now run ./install.sh."
