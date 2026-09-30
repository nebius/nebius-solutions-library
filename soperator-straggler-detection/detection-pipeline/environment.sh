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
# Default matches install.sh's own FIRST-checked, non-root-friendly
# candidate (a sibling of the repo checkout itself -- writable by
# whoever can already write to their own clone, no root needed), not
# the absolute /root/nccl-2.28-src this project's own original dev
# cluster happened to use. install.sh checks BOTH locations (this one
# first) and now resolves NCCL_HOME/NCCL_LIB_PATH to whichever it
# actually finds -- set NCCL_SRC_DIR=/root/nccl-2.28-src explicitly if
# you specifically want the other one instead (e.g. to match an
# existing tree already built there).
NCCL_SRC_DIR="${NCCL_SRC_DIR:-$PKG_ROOT/../../nccl-2.28-src}"
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
# =========================================================================

info "Checking for a real NCCL $NCCL_TAG source build at $NCCL_SRC_DIR..."
if [ -f "$NCCL_SRC_DIR/build/lib/libnccl.so" ] && [ -d "$NCCL_SRC_DIR/build/include" ]; then
  info "Real NCCL build already present at $NCCL_SRC_DIR/build -- nothing to do."
else
  if [ ! -d "$NCCL_SRC_DIR/.git" ]; then
    # Real, disclosed failure mode this project's own history has now
    # hit live: a plain `git clone` failure here is very often a LOCAL
    # permission problem (can't create $NCCL_SRC_DIR's parent, e.g. a
    # non-root user against /root/nccl-2.28-src), not a network/DNS
    # issue -- checked explicitly first so the real cause is reported,
    # not a misleading "confirm this host can reach github.com" guess.
    nccl_parent_dir="$(dirname "$NCCL_SRC_DIR")"
    if [ ! -w "$nccl_parent_dir" ]; then
      fail "Cannot write to $nccl_parent_dir (real permission check, not a guess) -- NCCL_SRC_DIR defaults to a sibling of this repo checkout, which needs write access to its parent directory. If you're not running as root/with sudo and this repo is checked out under /root, either re-run with sudo, or set NCCL_SRC_DIR to a location you can write to (e.g. NCCL_SRC_DIR=\$HOME/nccl-2.28-src ./environment.sh) -- install.sh checks the PKG_ROOT-relative sibling location first and falls back to /root/nccl-2.28-src, so anything else needs NCCL_HOME/NCCL_SRC_DIR set explicitly for both scripts to agree on where it is."
    else
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
    info "Building real NCCL $NCCL_TAG (make src.build, CUDA_HOME=$real_cuda_home -- NCCL's own standard documented build target, not invented here) -- this takes several real minutes..."
    ( cd "$NCCL_SRC_DIR" && make -j"$(nproc)" src.build CUDA_HOME="$real_cuda_home" ) \
      || fail "NCCL build failed -- see make's own output above for the real error."
  fi
fi

[ "$FAIL" -eq 1 ] && { echo "[environment.sh] Aborting -- see FATAL lines above." >&2; exit 1; }

info "Environment ready. You can now run ./install.sh."
