# OpenFold2 production canary using the one-shot Dynamo worker

This directory contains the offline renderer and evidence contract for one
production-shaped OpenFold2 cold-start canary, plus an explicit provisioned-node
runner. The Python tools do not call Kubernetes, Nebius, a registry, or any
external network. `run_provisioned_trial.sh` is the sole live orchestration
entry point.

The measured path is:

1. persist `target-submit-at.txt` immediately before submitting the target;
2. let Kubernetes schedule a one-container, exact-digest OpenFold2 placeholder;
3. bind the API-defaulted live Pod spec, UID, container ID, cgroup, IP, image,
   and node;
4. immediately create one tokenless CPU probe Job, which polls readiness while
   the restore path proceeds;
5. run one privileged, node-affine, no-retry restore Job;
6. require the restored target's exact readiness response and resolve the
   run-scoped ClusterIP Service to that exact Pod UID; and
7. make exactly two distinct, semantically validated OpenFold2 calls.

`evidence.py` reports successful semantic HTTP readiness, each inference call,
and `demand_to_two_semantic_responses` (ending at case 2
`response_received_at`, never validator finish); Kubernetes Pod Ready and worker timing
remain separate diagnostics. It refuses incomplete, failed, cross-run, or
wrong-UID evidence.

The exact OpenFold2 target image remains:

```text
registry.example.invalid/faststart/openfold2@sha256:fc64916731fee39e124225829dca78e80ec24fe1891be47057d0d69209b93ab4
```

That reference is the public placeholder. Set `OF2_TARGET_IMAGE` to an
accessible mirror only when it retains the same immutable digest. A runtime
worker contract may likewise be supplied with `OF2_CONTRACT_PATH` and its
matching `OF2_EXPECTED_CONTRACT_SHA256`; no registry credential is checked in.

## Inputs

For a new run, copy and complete these receipts:

- `restore-interface.example.json`: immutable worker and Python probe images,
  approved one-shot interface, and public-source/tool provenance;
- `run.example.json`: unique run, demand timestamp, exact existing H100 node,
  checkpoint identity, and separate read-only checkpoint and preloaded NIM-cache
  PVCs.

`restore-interface.example.json` is intentionally `approved: false`; it cannot
render a deployable manifest until the locally built worker and probe images
have immutable registry digests and the approval fields are completed.

`restore-interface.live.json` is the current immutable, approved contract. It
contains image and source digests, but no credential material. The current
probe accepts the two observed successful `/v1/health/ready` JSON shapes:
literal `true` or `{"status":"ready"}`. It then issues exactly two inference
calls. The current response-boundary n=3 measurements and validator digest are
recorded in `../performance/openfold2/README.md`; raw evidence payloads stay
outside Git.

The target is scheduler-created: its template uses required hostname affinity
and never `spec.nodeName`. The worker and target use the measured single-H100
resource envelope. The worker requests 500m CPU for scheduling but retains a
4-CPU limit, allowing the one-shot restore to fit beside the explicitly
preserved CPU-only storage holders. The semantic Job requests CPU and memory
only; it has no GPU resource.

The worker verifies and injects its baked `/snapshot-binaries` bundle. No
redundant tools PVC or target `LD_LIBRARY_PATH` is used. The target's read-only
cache PVC must contain the exact cache tree used by the checkpoint donor.

## Reproducible provisioned-node run

Create an evidence root with an existing `runs/` directory, then supply every
machine-specific input explicitly:

```console
mkdir -m 0700 -p /private/evidence/openfold2-native/runs

./run_provisioned_trial.sh \
  --run-id p9-earlyprobe \
  --evidence-root /private/evidence/openfold2-native \
  --node gpu-node-b.example.invalid \
  --kubeconfig /private/kubeconfig \
  --artifact-holder openfold2-artifact-holder-example \
  --cleanup
```

The runner pins `restore-interface.live.json` by SHA-256, verifies the current
validator SHA-256, requires the kubeconfig's minified API server to equal the
approved cluster endpoint, and requires the supplied artifact-holder Pod to be
Ready on the requested node. Only then does it atomically create the new
`EVIDENCE_ROOT/runs/RUN_ID` directory. Existing run directories are never
reused.

Omit `--cleanup` to retain the run-scoped resources for inspection. When it is
present, cleanup occurs only after PASS evidence is generated and deletes the
exact probe, restore-worker, and target manifests rendered into that run's
evidence directory. A failed run retains partial evidence and its resources for
diagnosis.

## Render and bind

Render the target Pod, run-scoped ClusterIP Services, and NetworkPolicies:

```console
python3 render.py target \
  --contract restore-interface.approved.json \
  --run-config run.json > target.yaml
python3 lint_manifest.py target.yaml
```

After the Pod is scheduled and its inert container is Running, capture the
complete Pod JSON. Then compute the same canonical hash as the worker:
recursively key-sorted, compact UTF-8 JSON of the live `.spec`, after API
defaulting and scheduling.

```console
python3 bind_target.py \
  --contract restore-interface.approved.json \
  --run-config run.json \
  --pod-json target-pod.json \
  --collected-at 2026-08-18T12:34:56.123456Z \
  --binding-output binding.json \
  --patch-output target-pod-spec.patch.json
```

The allowlisted H100 nodes use the already-observed cgroup-v2 systemd layout
with containerd. The binder derives the Burstable cgroup from the API-visible
Pod UID and full container ID, so no second privileged observer is needed. The
one privileged restore worker resolves the live cgroup and rejects a mismatch
before invoking restore.

`target-pod-spec.patch.json` is an RFC 6902 patch. Its first operation tests the
Pod UID, and its second adds
`archvteams.nebius.ai/target-pod-spec-sha256`. Apply it with content type
`application/json-patch+json`, capture the patched Pod again, and confirm that
the annotation equals `binding.json.pod_spec_sha256` before creating the
worker.

## Early probe and exactly two semantic requests

Immediately after binding and patching the target identity, render and create
the separate CPU probe before creating the restore worker:

```console
python3 render.py probe \
  --contract restore-interface.approved.json \
  --run-config run.json \
  --binding binding.json > semantic-probe.yaml
python3 lint_manifest.py semantic-probe.yaml
```

The probe mounts the reviewed `../validate_openfold2.py` source by its fixed
SHA-256, invokes it directly (no shell or retry wrapper), and supplies exactly
two unique run IDs. It polls the exact readiness route while restore runs, then
issues one POST per ID and requires two full, semantically valid structures.
Its single-line stdout is both the Job log receipt and the
`semantic-summary.json` input below.

Then render the only privileged workload in the run:

```console
python3 render.py restore \
  --contract restore-interface.approved.json \
  --run-config run.json \
  --binding binding.json > restore-worker.yaml
python3 lint_manifest.py restore-worker.yaml
```

The worker Job is `backoffLimit: 0`, requests no GPU, uses required affinity to
the bound target node, and passes the exact PodSpec digest via
`--target-pod-spec-sha256`. A successful worker emits one
`dynamo-one-shot-restore-receipt/v1` JSON object.

## Evidence and timings

Capture the final target Pod, canary Service, Service-owned EndpointSlices,
worker Job/Pod and JSON receipt, probe Job/Pod and semantic summary. Then run:

```console
python3 evidence.py \
  --contract restore-interface.approved.json \
  --run-config run.json \
  --binding binding.json \
  --target-pod target-final.json \
  --service canary-service.json \
  --endpoint-slices canary-endpointslices.json \
  --worker-job worker-job.json \
  --worker-pod worker-pod.json \
  --worker-receipt worker-receipt.json \
  --probe-job probe-job.json \
  --probe-pod probe-pod.json \
  --semantic-summary semantic-summary.json \
  --target-submit-at target-submit-at.txt \
  > canary-evidence.json
```

The v2 evidence receipt records successful semantic HTTP readiness from
`ready_wait.finished_at`, Kubernetes Pod Ready under its own key, both request
latencies, and demand-to-two-semantic-responses as a cross-check. Worker receipt
and semantic probe are independent concurrent timelines; neither is ordered
against the other. Keep raw Kubernetes JSON and Job logs beside this derived
receipt; repeat the measured run at least three times and report every run.
Whole-second Kubernetes creation, scheduling, Ready, Job, and container
timestamps receive only bounded same-second precision normalization; the
application HTTP timestamps used for the published metrics are never rebased.

## Offline verification

```console
python3 -m unittest discover -s tests -v
python3 -m py_compile render.py lint_manifest.py bind_target.py evidence.py
bash tests/test_run_provisioned_trial.sh
bash -n run_provisioned_trial.sh tests/test_run_provisioned_trial.sh
shellcheck -x run_provisioned_trial.sh tests/test_run_provisioned_trial.sh \
  tests/provisioned-fixtures/bin/kubectl \
  tests/provisioned-fixtures/bin/python3
```

The shell test substitutes local fake `kubectl` and Python entry points; it
makes no cluster or network request. The Python renderers remain offline.
