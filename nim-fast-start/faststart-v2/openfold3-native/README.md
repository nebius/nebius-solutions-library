# OpenFold3 production-shaped native fast-start lane

Status: native capture and performance qualification are complete on the
provisioned H100 lane. The selected buffered artifact passed three independent
production-shaped trials and six strict semantic calls. The checked-in worker
contract remains performance-validation-only until a full `agent` compliance
release is built after the exact Jammy CUDA base receives a baseline SBOM.

## Frozen OpenFold3 identity

- NIM image:
  `nvcr.io/nim/openfold/openfold3@sha256:6286cc7c02247ed3efe42f0f1af6c2f6f6a680b1e5cae669512c44b636aa42d2`
- Qualification node: `gpu-node-a.example.invalid`, one NVIDIA H100.
- Direct checkpoint: `openfold3-native-f7-v1`, artifact version `1`.
- Buffered candidate: `openfold3-native-f7-v2-buffered`, artifact version `1`.
- Prediction route: `/biology/openfold/openfold3/predict`.
- Fixture: `fixtures/request-20aa.json`, 532 bytes, SHA-256
  `09b30bf2132e3764f99d4f417b47713cd6350bd332fe3100cceb1be11589f8ae`.
- Validator SHA-256:
  `c7ec22a6107d0fff36e17c4c9d1b8a6cf3f4efcc592215da05521f2b43d9cd4a`.
- Corrected response-boundary validator for requalification:
  `679b3e027b18e78b4646569e8c6395fb5f62c4647704bb5089aa2385a20d11f5`.

The fixture is the retained 20-residue, query-only A3M request with one
diffusion sample. Every qualification trial makes exactly two actual HTTP
calls with different request and input IDs. A PASS requires HTTP 200, exact
call identity, one CIF structure, at least 100 `ATOM` rows, Cartesian atom-site
fields, and all five finite OpenFold3 scores.

## Warm-instance cold-start result

The response-boundary requalification completed three independent strict PASS
trials and six semantic calls. Each call timer ends after the complete HTTP
body is received, and each run retains call 2's absolute
`response_received_at`. The exact end-to-end total is therefore computed for
each run as T0 through that response boundary; it is not reconstructed by
adding medians and does not use validator completion.

The primary clock starts immediately before target Pod creation on an already
provisioned H100 with both storage volumes attached. HTTP readiness is the first
successful application readiness response observed by the independent probe;
it is not the Kubernetes Pod `Ready` condition. The two semantic calls follow
immediately, with distinct IDs and inputs. Call 1 therefore includes any
deferred model/JIT work, while call 2 is the warm call.

The selected buffered artifact was fully read before T0 so its exact
9,263,246,107-byte payload across 148 regular files was page-resident. Its tree
SHA-256 is
`f488019348551f356a153ce17cd9568a9d59497ead375c81a84ddef3bc3972c2`.
That prewarm is deliberately outside the measured interval and is reported as
part of the storage state, not hidden as ordinary attached storage. The fresh
full read took 7.386615 seconds. The direct comparator bypasses the page cache.

| Path | n | T0 to HTTP ready | T0 to Kubernetes Ready | Call 1 HTTP response | Call 2 HTTP response | Exact T0 through call 2 | Worker restore |
|---|---:|---:|---:|---:|---:|---:|---:|
| Buffered, attached storage, fully prewarmed | 3 | **12.271182 s** | 12.887492 s | **9.098247 s** | **9.166892 s** | **30.564921 s** | 4.924 s |

The selected values are medians. Their raw arrays and ranges are:

- HTTP ready: 12.088885, 12.271182, 12.369170 seconds;
- Kubernetes Ready diagnostic: 12.651241, 12.966096, 12.887492 seconds;
- call 1: 9.098247, 9.180301, 9.070079 seconds;
- call 2: 9.166892, 9.112610, 9.174043 seconds;
- exact T0 through call 2: 30.354807, 30.564921, 30.614101 seconds; and
- restore worker: 4.925, 4.924, 4.915 seconds.

The direct row remains a single historical storage-cold comparator: 87.284431
seconds to HTTP ready, 88.224833 seconds to Kubernetes Ready, 8.611488 and
8.548598 seconds for the two calls, 104.445954 seconds through legacy
validation completion, and 79.997 seconds in the restore worker. It does not
have an exact call-2 total and is not presented as an n=3 median.

The selected aggregate is
`<private-evidence-root>/openfold3-response-boundary/aggregate.json`,
SHA-256
`a8c8469759452aaf709aeeb5200e5b773337bae85788a94b7384e8f862d244f3`.
The full-read receipt SHA-256 is
`4e2ce483ed27d817f8e00fc26ef7f53fb9ad2b35f094b59ca44f97fb56abc7e9`;
the exact three-image residency receipt SHA-256 is
`e456d8410c95dc3bca4f0b43086de364523a5f876eada374d0fc1cd24aa0f613`.
All three trials recorded zero post-T0 pull events and zero terminal fault
events. Their cleanup receipts report no remaining run-scoped resources and
zero active GPU requests. The final-state receipt preserves the Ready holder
and both attached PVCs and has SHA-256
`914ef402442db45e10419a5958ceb57eaa49645d4fe96d1764d1f8ea5037fffe`.

The fresh capacity audit found 4,830m of existing CPU requests against 15,900m
allocatable. The original 1,000m worker request would have left -30m after the
10,000m target and 100m probe, so that preflight failed before mutation. The
selected worker reserves 500m with the same 4-CPU execution limit and leaves
470m headroom; the manifest linter pins both values.

One earlier cohort was excluded before aggregation when Kubernetes' whole-second
probe-finish timestamp exposed a 0.614789-second precision inversion against
the validator's sub-second completion time. The verifier now normalizes only a
sub-second Kubernetes quantization edge and still rejects an inversion of one
second or more. The excluded run was cleaned and was neither reused nor counted.

## Retained H100 baseline

`prior-evidence.json` is a digest-bound summary of the existing raw result.
The raw evidence measured a conventional ready time of 202 seconds from Pod
creation (181 seconds from container start). The model cache contained
3,305,746,737 bytes. The selected buffered checkpoint identity in that compact
summary is sourced from the authoritative receipt above: 148 regular files,
9,263,246,107 bytes, manifest SHA-256
`5df221e0736a4c6f369781ea0dbc7c36783c26d3f35dcd874b4ced8f5f9e009f`,
and tree SHA-256
`f488019348551f356a153ce17cd9568a9d59497ead375c81a84ddef3bc3972c2`.

The earlier retained-page-cache experiment reported these `n=3` medians:

| Measurement | Median |
|---|---:|
| CRIU restore | 4.249 s |
| HTTP ready | 5.454 s |
| First semantic response | 8.375062 s |
| Second semantic response | 8.474428 s |
| Restore through two responses | 22.298048 s |

A cold-root-disk trial took 67.978 seconds in CRIU and 90.322516 seconds
through two responses. These values are experimental baselines, not the new
production-shaped number. The new runner begins its demand clock before target
creation and requires a UID/PodSpec-bound worker, a run-scoped ClusterIP, and
two strict semantic responses.

The retained donor evidence also proves that `uvloop` was absent at capture.
The new donor therefore explicitly uninstalls `uvloop` before starting the
server and performs two strict loopback predictions before becoming Ready.

## Release contract and current blocker

`dynamo/restore-interface.live.json` is the single source for the immutable
capture/restore worker image, executable and tool receipts, exact eight-patch
source inputs, argument contract, supported image-I/O modes, classification,
and release approval. Its current candidate is the integrated, exact-source
portable-plus-buffered performance-validation build:

```text
registry.example.invalid/faststart/snapshot-agent@sha256:d5ce1eaad55378a93a9bf53b35effcbc378ed15ab7e5b7f6b41df6689cefdf28
```

Its restore-worker SHA-256 is
`941157dd1815acf6f3e26cbe9dea65ee1c9a398c719881d474e5d7c5c7e28651`,
its 34-file tool-manifest SHA-256 is
`c0d638100c03fa35973e82859d15b9c8dd1bcbf0fe9cb185b58cc21fae7ead1e`,
and its GLIBC-2.35 compatibility receipt SHA-256 is
`f7af5b214cb963c4cf64910dfafe16987f0c5ec886af5d0e5d7aab5b634f6950`.
The image supports direct, writeback, and buffered I/O; this lane qualifies the
explicit direct and buffered artifact identities.

The current image is classified `performance-validation-only`, not as a full
compliance release, so `release_ready` remains `false`. Capture and trial
commands refuse it by default. Performance qualification may explicitly opt in
with `--allow-performance-validation-worker`; the renderer and runner then
require and retain that exact non-release classification and nonempty blocker.
Production release still requires a final full `agent` compliance image and
receipts, formal approval, classification `full-agent-compliance-release`, an
empty blocker, and `release_ready: true`.

## Prepared implementation

- `donor-job.yaml` and `storage.yaml`: exact-image donor, two warm-up calls,
  measured target glibc receipt, 64 GiB `/dev/shm`, and isolated cache/artifact
  PVCs.
- `render_snapshot_agent.py`, `snapshot-agent.yaml.tmpl`,
  `render_capture.py`, and `podsnapshotcontent.yaml.tmpl`: release-gated worker
  selection plus exact Pod name, UID, image, node, and Ready-state capture.
- `artifact-holder.yaml`: four-reader hash and full prewarm of the immutable
  direct artifact.
- `render_buffered_variant.py` and `artifact-holder-buffered.yaml`: a
  receipt-authorized, write-once, hard-linked variant whose distinct manifest
  explicitly selects buffered image I/O.
- `validate_openfold3.py`: strict two-distinct-call semantic validation through
  a plain-HTTP ClusterIP origin with proxies and redirects disabled.
- `dynamo/`: scheduler-created inert GPU target, live UID/container/cgroup/IP
  and canonical PodSpec binding, early CPU probe submitted before the worker,
  exact direct/buffered artifact tuples, evidence construction, and an `n=3`
  aggregator covering six semantic calls.

The exact capture, qualification, and replay procedure is in
`EXECUTION_PLAN.md`.

## Offline verification

Run from this directory:

```console
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m unittest discover -s dynamo/tests -p 'test_*.py' -v
bash dynamo/tests/test_run_provisioned_trial.sh
bash dynamo/tests/test_run_n3.sh
python3 -m py_compile validate_openfold3.py render_capture.py \
  render_snapshot_agent.py render_buffered_variant.py dynamo/*.py
bash -n dynamo/run_provisioned_trial.sh dynamo/run_n3.sh \
  dynamo/run_response_n3.sh dynamo/tests/*.sh
shellcheck -e SC1091 dynamo/run_provisioned_trial.sh dynamo/run_n3.sh \
  dynamo/run_response_n3.sh dynamo/tests/*.sh
```

These checks make no Kubernetes, cloud, registry, or external-network call.
