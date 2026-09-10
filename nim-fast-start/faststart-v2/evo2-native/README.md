# Evo2-40B native H200 fast-start lane

This directory is the offline-prepared, production-shaped path for restoring
Evo2-40B on the previously used single-H200 resource. It does not claim a new
measurement: H200 capture and live execution are deliberately deferred. The
retained legacy evidence establishes the workload contract and a useful lower
bound; a new manifest-bound native artifact is still required before either
candidate can run.

## Live qualification status (2026-08-18)

Read-only preflight against the only allowed cluster confirmed that the pinned
H200 is Ready and has the exact Evo2 digest cached, but it is occupied by the
healthy owner-managed `nim-fast-start/evo2-40b` Deployment. That Pod requests
the node's single full GPU plus 15 CPU and 180 GiB. It is not a task-scoped
capture donor, and its `/root/.cache/ngc` mount and 32 GiB shared-memory layout
do not match this lane's final topology. No Deployment, Pod, PVC, Service, or
snapshot object was changed.

The one external action required before qualification is an explicit owner
decision to release that Deployment's sole H200 (for example, an authorized
temporary scale to zero). Once the node has zero active GPU requests, follow
the capture sequence below. The runner then fails closed unless the node,
three exact images, attached PVCs, holder receipt, and requested storage state
are all present before T0.

## Exact workload contract

- NIM image: `nvcr.io/nim/arc/evo2-40b@sha256:561886bab1d2d0da836ebf5bec403f9de2baf6e92deb7eedf1b316aa994b5dd2`
- deployment profile: `gpu-h200-sxm/1gpu-16vcpu-200gb`
- topology: one full, non-MIG NVIDIA H200 SXM, compute capability 9.0,
  141 GB marketed HBM capacity
- retained capture node: `h200-node.example.invalid`
- target resource envelope: 4 CPU/32 GiB requested, 32 CPU/180 GiB limited,
  one full GPU, and 64 GiB `/dev/shm`
- cache mount: `/opt/nim/.cache/ngc`, with `NIM_CACHE_PATH=/opt/nim/.cache`
- endpoint: `POST /biology/arc/evo2/generate`
- readiness: `GET /v1/health/ready`

`profile.json` is the machine-readable source of these values. Renderers and
runners reject a different image, node, storage claim, checkpoint identity,
GPU count, or I/O mode. The live runner also requires the node to report one
allocatable full `nvidia.com/gpu`, the H200 label, no MIG resources, and no
other active GPU request.

The retained 2026-08-17 evidence is under
`<private-evidence-root>/evo2-h200-faststart-20260817T234216Z`.
Its 99,959,572,798-byte legacy checkpoint completed three explicit
cache-drop/O_DIRECT trials in a 67.390-second median through the second valid
generation. That artifact predates the current manifest-bound one-shot worker,
so it is evidence for the profile and optimization, not a deployable artifact
for this lane.

## Native capture and candidates

`render_capture.py` emits four offline-reviewed object sets:

1. isolated 256 GiB M3 artifact and 150 GiB NIM-cache PVCs;
2. a node-affine native snapshot agent using the current exact worker image;
3. an exact-digest donor that starts the real NIM, performs two strict seeded
   generations, and only then exposes `ready-for-snapshot`; and
4. a UID-bound `PodSnapshotContent` after the donor Pod exists.

The direct capture ID is `evo2-40b-native-h200-v1`, version `1`.
`artifact_variant.py` verifies its post-capture manifest digest and complete
inventory, hard-links every payload file, and changes only the checkpoint ID
and `criu.imageIoMode` to create
`evo2-40b-native-h200-v1-buffered`. It never overwrites a destination.

`prewarm_artifact.py` validates both candidates. Direct mode inventories the
artifact without pulling its 100 GB payload into ordinary page cache;
buffered mode reads every byte before setting the holder Ready marker. This
keeps the two hypotheses explicit:

- `direct`: native O_DIRECT/AIO restore, independent of ordinary page-cache
  residency;
- `buffered`: legacy buffered CRIU restore from a deliberately resident
  artifact, intended to find the provisioned-node performance floor.

Example offline rendering (these commands make no Kubernetes or network call):

```console
python3 render_capture.py storage > /tmp/evo2-storage.yaml
python3 render_capture.py donor --capture-id h200-r1 > /tmp/evo2-donor.yaml
python3 render_capture.py agent --capture-id h200-r1 > /tmp/evo2-agent.yaml
python3 render_capture.py content \
  --capture-id h200-r1 \
  --source-pod e2-donor-h200-r1-abcde \
  --source-uid 11111111-1111-4111-8111-111111111111 \
  > /tmp/evo2-content.yaml
```

After capture, record the direct manifest SHA-256, file count, and total bytes;
use those exact values to build the buffered variant and render one holder per
mode. `profile.json` intentionally leaves both manifest digests `null` until
that evidence exists. Do not substitute the legacy checkpoint's values.

## Measured provisioned-node path

`run_one_provisioned_trial.sh` implements one complete demand edge:

1. verify the exact API server, one-H200 topology, zero existing GPU demand,
   immutable worker/profile inputs, and mode-specific Ready artifact holder;
2. record demand and create a scheduler-bound inert target from the exact NIM
   digest;
3. bind the API-defaulted live Pod UID, full container ID, image ID, cgroup,
   IP, node, and canonical PodSpec SHA-256;
4. create a separate tokenless CPU probe before the restore worker;
5. let that probe poll the run-scoped ClusterIP while the one-shot worker
   restores the process tree; and
6. require exactly two distinct semantic POSTs, eventual Kubernetes Ready,
   the exact Service endpoint UID/IP, and a successful worker receipt.

The strict calls retain the proven top-k-one workload:

| call | input | seed | required 20-token output |
|---:|---|---:|---|
| 1 | `ATCGATCGATCG` | 2407001 | `ATCGATCGATCGATCGATCG` |
| 2 | `GATTACAGATTACA` | 2407002 | `GATTACAGATTACAGATTAC` |

The validator rejects redirects and proxies, non-200 responses, malformed or
non-DNA output, different deterministic sequences, missing/invalid 20-element
per-token timings, unexpected logits/probabilities, duplicate run IDs, or any
request count other than two. Inference is never retried.

`run_provisioned_n3.sh` runs three fresh target UIDs serially for one I/O mode
and calls `aggregate_results.py`. The aggregate fails closed unless all three
runs share the exact image, checkpoint, manifest and topology, contain six
strict semantic passes, and have unique run and Pod IDs. Direct and buffered
must be run as separate n=3 sets; compare their medians, not their fastest
single trials.

Each trial and aggregate exposes demand to successful semantic HTTP readiness,
demand to Kubernetes Pod Ready, the first inference call, the immediate warm
second call, and demand to completion of call 2. `T0` is before target creation
on the already provisioned H200 with storage attached. The HTTP timestamp comes
from the validator's successful readiness receipt; worker receipt and probe
events remain independent concurrent timelines.

More precisely, the authoritative T0 is the nanosecond timestamp written to
`target-submit-at.txt` immediately before `kubectl create` of the inert target;
the earlier `run.json:demand_at` is setup evidence only. The aggregate rejects
the legacy fallback. It also records that the target, restore-worker, and probe
images were already cached, both PVCs were attached through the Ready holder,
and either (a) direct/O_DIRECT payload pages were not preloaded or (b) the
entire buffered artifact was read before T0. Buffered prewarm duration and
completion time are reported but excluded from the cold-start interval.

## Worker release gate

The exact current eight-patch worker is
`snapshot-agent@sha256:d5ce1eaad55378a93a9bf53b35effcbc378ed15ab7e5b7f6b41df6689cefdf28`.
Its restore-worker SHA-256 is
`941157dd1815acf6f3e26cbe9dea65ee1c9a398c719881d474e5d7c5c7e28651`
and its tool-manifest SHA-256 is
`c0d638100c03fa35973e82859d15b9c8dd1bcbf0fe9cb185b58cc21fae7ead1e`.
It supports direct, writeback, and buffered modes, and its 34-file bundle has
an in-build GLIBC ceiling of 2.35. The eighth source patch builds
`ns-bind-mount` against the Jammy/GLIBC-2.35 worker runtime.

This image is approved only for performance validation. `worker-gate.json`
sets `release_ready` to false because the full `agent` compliance target still
needs a baseline SBOM for the exact Jammy CUDA runtime base. Live performance
runs therefore require the explicit
`--allow-performance-validation-worker` flag. Production promotion must not
use that override; it requires a new full-compliance image and an updated,
reviewed gate receipt.

## Deferred work and acceptance

No live H200, Kubernetes, cloud, or registry operation was performed while
preparing this lane. Completion of this model requires, in order:

1. confirm the retained one-H200 node and isolated storage are available;
2. capture the new native direct artifact and retain donor-before/after
   semantics, snapshot content, agent logs, manifest, and inventory;
3. create and verify the hard-linked buffered variant;
4. run direct n=3 and buffered-prewarmed n=3 through the same early ClusterIP
   probe; and
5. retain both aggregates and select the lower valid median.

Until those steps pass, Evo2's current result remains the retained 67.390 s
legacy direct baseline, not a newly qualified production result.

## Offline verification

```console
python3 -m unittest discover -s tests -v
python3 -m py_compile \
  validate_evo2.py render.py bind_target.py render_capture.py \
  artifact_variant.py prewarm_artifact.py aggregate_results.py
bash -n run_one_provisioned_trial.sh run_provisioned_n3.sh
shellcheck -x run_one_provisioned_trial.sh run_provisioned_n3.sh
```
