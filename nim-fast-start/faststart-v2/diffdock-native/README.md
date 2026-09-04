# DiffDock production-shaped native fast start

Status: exact response-boundary requalification is complete on the provisioned
H100 lane. The selected fully prewarmed buffered checkpoint passed three fresh
runs and six strict 1UBQ-plus-aspirin dockings. The exact end-to-end boundary is
the persisted pre-create `T0` through the second call's
`response_received_at`; validator completion is not included.

## Winning result

- Approved cluster: `mk8scluster-example`
- Node: `gpu-node-a.example.invalid`
- NIM image:
  `nvcr.io/nim/mit/diffdock@sha256:300696eb8331d78face40f84d835cc1e278c7d3c391c5aabbbee5884366da480`
- Checkpoint: `diffdock-native-f7-v3-buffered`, version `1`
- Manifest:
  `93a83188fb0adcc89c1278f136595c6dbce1b3fe9c412c3ccf65f704745ec1fe`
- Inventory: 122 regular files, 7,516,058,314 bytes
- Restore worker:
  `registry.example.invalid/faststart/snapshot-agent@sha256:063286a3a1354d1c5969fa80f445bb5fbd2a96bc0999c7b6897495f0b4c2fd4d`
- Worker executable SHA-256:
  `941157dd1815acf6f3e26cbe9dea65ee1c9a398c719881d474e5d7c5c7e28651`
- Injected tool manifest:
  `fc22c423deca17b4175ab42c23a66310c8e2c4d8c4b63a24c33894300020943b`

| mode | run | HTTP ready (s) | Kubernetes Ready (s) | call 1 HTTP response (s) | call 2 HTTP response (s) | exact T0 to call 2 body (s) | restore (s) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| buffered | `dd-rb-1209-r1` | 12.127239 | 12.858719 | 1.462333 | 0.599702 | 14.190621 | 4.463 |
| buffered | `dd-rb-1209-r2` | 12.057153 | 12.702105 | 1.456961 | 0.588161 | 14.103816 | 4.366 |
| buffered | `dd-rb-1209-r3` | 12.181481 | 12.674250 | 1.456592 | 0.578353 | 14.217744 | 4.405 |
| **median** | n=3 | **12.127239** | **12.702105** | **1.456961** | **0.588161** | **14.190621** | **4.405** |

All three runs passed two distinct strict semantic calls through a run-scoped
ClusterIP: 6/6 requests passed. The ranges are 12.057153–12.181481 seconds for
HTTP readiness, 12.674250–12.858719 for Kubernetes Ready,
1.456592–1.462333 for call 1, 0.578353–0.599702 for call 2, and
14.103816–14.217744 for the exact end-to-end total.

Here HTTP ready is the validator's first successful semantic readiness
response. Kubernetes Pod Ready is retained as a separate diagnostic. `T0` is
captured immediately before target creation on the provisioned H100 with both
PVCs already attached. The call timers run from request dispatch through the
complete HTTP body. The first call includes any deferred model work; the second
is the immediate warm inference. The older `dd-buf-p*` cohort remains only as a
legacy validation-completion comparator.

## Artifact construction

The UID-bound source capture is `diffdock-native-f7-v1`: 122 files,
7,516,058,303 bytes, manifest
`b1c477efdfc6bcb8e253462524cef24fef6e059f43c97a1fcb94b85dca81e0b8`.
Capture took 92.543 seconds, including 90.874 seconds in CRIU and 1.649
seconds in CUDA checkpointing.

`artifact-buffered-variant.yaml` publishes the winning variant atomically. It
hard-links all 121 payload files (7,516,052,518 bytes), preserves the exact
184,320-byte rootfs delta, and changes only the checkpoint identity and
`imageIoMode` from `direct` to `buffered`. Immediately before the selected
cohort, the unchanged holder freshly hashed all 122 files and
7,516,058,314 bytes in 5.931160 seconds. The receipt binds tree SHA-256
`2d9e339392d6b4c5207ddbd4ef8f26465e324b2e165bd4cd9b43530f006e1b1d`.

The exact target, restore-worker, and probe images were preloaded with zero GPU
requests, their resolved image IDs were captured, and the setup Pod was
UID-precondition deleted before `T0`. The setup-only target pull transferred
17,047,526,597 bytes in 277.895 seconds; it is explicitly excluded.

## Semantic and cleanup contract

The request fixture is the retained full 1UBQ receptor plus aspirin request:
79,668 bytes, SHA-256
`f58c2b74f534529a3b7e5cdd1410e8df33a25cee64a988a62170c5c69ca80977`.
Each measured run submits exactly two real requests from a separate CPU probe.
Every response must preserve the submitted receptor and ligand, contain one
finite 13-atom V2000 pose, one finite confidence, and a trajectory.

All target, probe, worker, Service, RBAC, ConfigMap, and NetworkPolicy objects
from the three selected runs were removed using exact object UID preconditions.
Each cleanup receipt proves zero run-scoped objects and zero active GPU requests.
The immutable DiffDock artifact, cache PVC, and holder remain on the selected
provisioned node. The
ProteinMPNN, MSA Search, OpenFold3, and unrelated holders were preserved.

The excluded setup attempts are retained in raw evidence: donor r1 executed no
request because Python 3.10 lacks `datetime.UTC`; holder r1 used no GPU and
could not traverse the capture worker's mode-0700 artifact tree. The later
`dd-image-api-test-1201` was a setup-only image pull and UID-delete rehearsal
with no demand T0 and no inference. None contributes to the selected cohort.

Exact machine-readable results are in `results.json`. Raw receipts are retained
outside Git at
`<private-evidence-root>/diffdock-native-f7-response-20260818T1209Z`.
The aggregate SHA-256 is
`1e582f6c571e5d9af36e362b2f75df43fef035b7a7265780a5052e2531e88f24`;
the prewarm, image-residency, and final-state receipt digests are recorded in
`results.json`.

`compatibility-evidence.json` also remains byte-for-byte hash-bound to its
historical review. Its old `demand_to_two_semantic_responses_seconds` key names
a validation-complete value; it is not selected. `results.json` v3 carries the
authoritative exact response-boundary result.

## Verification

```console
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m unittest discover -s dynamo/tests -p 'test_*.py' -v
bash dynamo/tests/test_run_provisioned_trial.sh
python3 -m py_compile validate_diffdock.py render_capture.py \
  rootfs_variant.py render_rootfs_variant.py prewarm_buffered_artifact.py \
  dynamo/*.py
bash -n dynamo/run_provisioned_trial.sh dynamo/run_response_n3.sh \
  dynamo/tests/test_run_provisioned_trial.sh
shellcheck dynamo/run_provisioned_trial.sh dynamo/run_response_n3.sh
```
