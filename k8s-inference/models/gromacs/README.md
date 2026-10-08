# GROMACS run class

The reference "run" workload of Serverless 2.0: the customer baseline system
(LynxKite "mas-20e", 185,486 atoms, CHARMM36 membrane protein with ligand,
semi-isotropic C-rescale NPT, 1 us = 500,000,000 steps at dt 2 fs).
Numbers: `spikes/S9-gromacs-baseline/RESULT.md` (baseline) and
`spikes/S16-gromacs-rtx-tuning/RESULT.md` (RTX PRO 6000 tuning, multi-GPU,
CPU-only cost study; the defaults below come from it).

| File | Purpose |
|---|---|
| `catalog/models/gromacs.yaml` | the run class itself: the `job` block (grompp once, mdrun with checkpoints), parameters, GPU classes, per-region prices (`docs/JOBS.md`) |
| `image/Dockerfile` | the GROMACS image: 2026.4, CUDA 12.8, sm_90 (H100) + sm_120 (RTX PRO 6000); build args `GMX_VERSION`, `CUDA` |

Inputs (gro, top + toppar/, mdp, ndx; 24 MB) are in the bucket
`s3://serverless2-gromacs-baseline/inputs/mas-20e/` and are not tracked in
git; a tenant copies them into its own bucket (`uploads/`) and passes
`input_prefix`. Benchmark templates, kernel-patch experiments and the
experimental Dockerfile live in `spikes/S16-gromacs-rtx-tuning/`.

## How it runs

Through the customer API only (`POST /v1/models/gromacs:invoke`, mode `run`):
the API renders the catalog entry into one Kubernetes Job in the tenant
namespace (runner `fetch` init container, `main` = this image, runner
`uploader`; per-run work volume with the checkpoints), Kueue admits it on the
model's scheduling profile (`prefer-rtx-pro-6000`: RTX PRO 6000 first, H100
when those are busy, never L40S), and on the control cluster the MultiKueue
dispatcher picks the cheapest free region (`docs/SCHEDULING.md`). Nothing is
applied by hand any more; the Argo WorkflowTemplate of the first version is
retired (lane F5, 2026-10-07).

```sh
curl -X POST $API/v1/models/gromacs:invoke -H "Authorization: Bearer $KEY" -d '{
  "name": "mas20e-1ns",
  "input": { "input_prefix": "s3://serverless2-<tenant>-eu-north1/uploads/mas-20e", "nsteps": 500000 } }'
```

Parameters: `input_prefix`, `nsteps`, `gro`, `top`, `mdp`, `ndx`, `update`
(gpu|cpu), `ntomp`, `nstlist`, `pin`, `extra_args`; `nb_min_ci` is a
per-region default of the catalog entry. Outputs (`run.xtc`, `run.edr`,
`run.log`, `run.cpt`, `run.gro`, `topol.tpr`, `mdout.mdp`, `run.mdp`,
`STATUS.json`, `attempts/`) land under `operations/<id>/` in the tenant
bucket through the uploader, which also runs after a failed or interrupted
attempt so partial results and the last checkpoint are kept.

The image is pushed to the fleet's registry (`cr.<region>.nebius.cloud/
<registry id>/gromacs:2026.4-cuda12.8-sm90-120`) and mirrored into
every other region under the same path (`docs/OPERATIONS.md` "Mirror an image
into a region"); the catalog names the hub reference, the API and the
dispatcher rewrite the registry prefix for the region a run lands in.

## Defaults and measured performance (S16)

`-ntomp 12 -pin off -nstlist 200`, CPU request 12 without a limit (a cgroup
quota below the node's logical CPU count disables GROMACS pinning and throttles
the pair search), GROMACS 2026.4, `nb_min_ci` 16000 where the catalog sets it:

| GPU | ns/day | 1 us | at spot list |
|---|---|---|---|
| RTX PRO 6000 (eu-south1) | 320-323 (S9 flags: 277) | 3.1 days | about $71 |
| H100 (hub) | 216 (S9 flags: 197) | 4.6 days | about $238 |

The run is GPU-bound (NB kernel 50%, PME 33% of GPU time); nothing
physics-preserving beats that, including custom CUDA kernels (S16 phases 2-3).
Multi-GPU is a NO-GO for this system (domain decomposition forces CPU update
for this topology: 2 GPUs 0.64x, 8 GPUs 1.2x); CPU-only nodes are 24-30x the
cost per microsecond. Hydrogen mass repartitioning (4 fs, about 1.9x) is a
model change the customer must approve and needs a re-thermalisation stage;
it is documented in the spike, not offered by this run class. The catalog
therefore prefers the RTX PRO 6000 class (about 3x cheaper per nanosecond).

## Behaviour

- `fetch` and the `uploader` are the runner image's containers in the same
  pod (`docs/JOBS.md`); only `main` needs the GPU.
- Checkpoint every 5 min (`-cpt 5`) on the run's 50 GiB ReadWriteOnce volume
  (`/work`). A spot interruption does not count against `backoffLimit`
  (podFailurePolicy); the replacement pod reattaches the volume in the same
  region, skips grompp when `topol.tpr` exists and `-cpi run.cpt` continues
  from the last checkpoint. `:resume` does the same by hand after a failure
  or a cancel.
- GPU-resident mode: `-nb gpu -pme gpu -bonded gpu -update gpu`, 1 rank.
  GROMACS accepts `-update gpu` for this system (md integrator, v-rescale,
  C-rescale semi-isotropic, h-bonds LINCS, linear COM removal with two
  groups); `update=cpu` is the fallback parameter for systems where it refuses.
  CMAP (CHARMM) stays on the CPU in upstream GROMACS; porting it to the GPU
  was measured in S16 and changes nothing.
- Image: 2026.4 with CUDA 12.8 for sm_90 and sm_120, about 930 MB. The NGC
  image `nvcr.io/hpc/gromacs:2023.2` works on H100 but fails on Blackwell at
  step 0 (`cudaErrorInvalidPtx`, no sm_120 code; NGC has no newer tag). The
  image has no sm_89 code, which is why the catalog's `gpu.classes` leave
  the L40S out: the class affinity keeps the preference queues from falling
  back to it.
- mdp: `nsteps` is the only edit (sed on a copy, `run.mdp`; the customer's
  `md.mdp` is untouched); `grompp` runs with `-maxwarn 0` and emits no warning
  for the baseline; `mdout.mdp` in the output prefix records the full
  processed parameter set.
