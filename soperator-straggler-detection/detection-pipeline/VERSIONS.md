# Pinned/confirmed versions — real values this package was validated against

This file exists because Stage 5's own real, isolated validation found no
single place recorded exactly what this package's validation run was
actually built and tested against. Every value below is a real, live-
confirmed reading from this project's own validation cluster (2-node,
H200), not a guess or a generic default — re-confirm on your own cluster
rather than assuming these carry over.

## GPU / driver

- **GPU**: NVIDIA H200
- **Driver**: `580.159.04` (`nvidia-smi --query-gpu=driver_version`)

## CUDA toolkit (host, used to build the Inspector plugin)

- `nvcc` release **13.0**, V13.0.88 (`nvcc --version`)

## NCCL

- **2.28.9** (`nccl-2.28-src/makefiles/version.mk`: `NCCL_MAJOR=2
  NCCL_MINOR=28 NCCL_PATCH=9`) — this is the source tree the Inspector
  plugin (`NCCL_PROFILER_PLUGIN`) is built against and the version every
  launch script's `NCCL_LIB_PATH` points at.
- **Read README.md's own Requirements section before assuming this is
  what actually runs**: the container mount convention most launch
  scripts use shadows the container's own bundled NCCL with whatever
  version is host-installed at `/usr/lib/x86_64-linux-gnu` — `install.sh`
  detects and reports the real, currently-linked host version per node
  at install time; this file is the Inspector-plugin build version, not
  necessarily the training runtime's own NCCL version. Two shapes
  (`long-context`, `rl`) additionally force `LD_LIBRARY_PATH` to this
  exact 2.28.9 build regardless of the host, a real, disclosed
  inconsistency (see README.md's own NCCL note).

## Inspector plugin (NCCL profiler)

- Dump format: `inspector_output_format_version: v4.0` (as written into
  every real dump file's own `metadata` header).
- **Git revision: not captured.** Every real dump file's own
  `metadata.git_rev` field is empty in this validated build. Root cause,
  confirmed this session: the plugin's `Makefile` (line ~57) generates
  this at build time via `./utils/extract_git_version.sh`, but that
  script **does not exist** in this deployed source tree
  (`nccl-2.28-src/ext-profiler/inspector/utils/` is absent) — the
  compiled `version.cc` instead ships a hardcoded empty-string stub.
  This is a real, disclosed gap in the Inspector plugin's own build
  process, not something this package's own scripts caused or can fix
  from outside that source tree — flagging here so a future rebuild
  knows to either restore that script or accept an untracked plugin
  revision.

## Container image (all 15 workload shapes)

- `nvcr.io#nvidia/pytorch:25.01-py3` — the one real, pinned image every
  launch script's `IMAGE`/`--container-image` uses. This image's own
  bundled PyTorch/torchvision/NumPy versions are the real Python
  dependency pins for every workload (see README.md's Requirements
  section) — this project does not separately pin Python package
  versions on top of this image, and this session did not independently
  re-extract the image's exact bundled package versions (a real gap;
  `docker run --rm nvcr.io#nvidia/pytorch:25.01-py3 pip freeze`, or the
  equivalent enroot/pyxis invocation, is the real way to get them if
  needed).

## Observability stack

- **VictoriaMetrics**: `victoria-metrics-20260814-123346-tags-v1.150.0`
  (see `vm-standalone/README.md`'s own confirmed-version note).
- **Grafana**: `11.5.1` (see `grafana-standalone/README.md`'s own
  confirmed-version note).

## External tools

- **bpftrace**: confirmed working at `0.20.2-1ubuntu4.3` (live: `bpftrace
  --version` → `v0.20.2`) — see README.md's Requirements section.
