# DiffDock native capture and buffered trial runbook

This runbook records the production-shaped sequence that completed on
`gpu-node-a.example.invalid`. Use only the task kubeconfig and stop if
its minified API server is not the approved `mk8scluster-example`
endpoint.

```console
: "${FASTSTART_V2_ROOT:?set FASTSTART_V2_ROOT}"
: "${FASTSTART_EVIDENCE_ROOT:?set FASTSTART_EVIDENCE_ROOT}"
: "${KUBECONFIG:?set KUBECONFIG}"
: "${EXPECTED_API_SERVER:?set EXPECTED_API_SERVER}"
: "${TARGET_NODE:?set TARGET_NODE}"
export DD_LANE=${FASTSTART_V2_ROOT}/diffdock-native
export DD_KUBECONFIG=${KUBECONFIG}
export DD_EVIDENCE=${FASTSTART_EVIDENCE_ROOT}/diffdock-native-f7-$(date -u +%Y%m%dT%H%M%SZ)
install -d -m 0700 "$DD_EVIDENCE" "$DD_EVIDENCE/runs"

test "$(kubectl --kubeconfig "$DD_KUBECONFIG" config view --minify \
  -o jsonpath='{.clusters[0].cluster.server}')" = \
  "$EXPECTED_API_SERVER"
kubectl --kubeconfig "$DD_KUBECONFIG" get node \
  "$TARGET_NODE" -o json > "$DD_EVIDENCE/node-before.json"
```

Require Ready=True, allocatable `nvidia.com/gpu=1`, and zero existing GPU
requests on hf93. Do not delete or modify another model lane's objects.

## Capture

The storage classes use `WaitForFirstConsumer`. Create storage, then the
artifact consumer; do not wait for both claims to bind before creating their
first consumers.

```console
kubectl --kubeconfig "$DD_KUBECONFIG" -n nim-fast-start create -f "$DD_LANE/storage.yaml"
kubectl --kubeconfig "$DD_KUBECONFIG" -n nim-fast-start create -f "$DD_LANE/snapshot-agent.yaml"
kubectl --kubeconfig "$DD_KUBECONFIG" -n nim-fast-start wait \
  --for=condition=Ready pod/diffdock-native-f7-snapshot-agent-hf93 --timeout=600s

kubectl --kubeconfig "$DD_KUBECONFIG" -n nim-fast-start create configmap \
  diffdock-native-f7-validator-r2 \
  --from-file=VALIDATOR_SOURCE="$DD_LANE/validate_diffdock.py" \
  --from-file=REQUEST_JSON="$DD_LANE/fixtures/1ubq-aspirin-request.json" \
  --dry-run=client -o yaml > "$DD_EVIDENCE/donor-configmap.yaml"
kubectl --kubeconfig "$DD_KUBECONFIG" -n nim-fast-start create \
  -f "$DD_EVIDENCE/donor-configmap.yaml"
kubectl --kubeconfig "$DD_KUBECONFIG" -n nim-fast-start create -f "$DD_LANE/donor-job.yaml"
```

Wait for the one donor Pod to become Ready. Readiness means the exact pinned
image completed two full 1UBQ-plus-aspirin requests. Preserve its Pod JSON,
logs, `/snapshot-control/target-runtime.json`, and semantic receipts. Render
the PodSnapshotContent only from that live Ready Pod:

```console
python3 "$DD_LANE/render_capture.py" \
  --donor-pod-json "$DD_EVIDENCE/donor-pod.json" \
  > "$DD_EVIDENCE/podsnapshotcontent.yaml"
kubectl --kubeconfig "$DD_KUBECONFIG" -n nim-fast-start create \
  -f "$DD_EVIDENCE/podsnapshotcontent.yaml"
kubectl --kubeconfig "$DD_KUBECONFIG" -n nim-fast-start wait \
  --for=condition=Ready podsnapshotcontent/diffdock-native-f7-v1-direct-hf93 \
  --timeout=1800s
```

After receipts are durable, delete only the DiffDock donor Job, snapshot-agent
Pod, and validator ConfigMap. The retained live source artifact has manifest
`b1c477efdfc6bcb8e253462524cef24fef6e059f43c97a1fcb94b85dca81e0b8`.
A new capture has a new identity and must be rebound into the exact source
holder and buffered builder before use.

## Direct canary and buffered publication

Create `artifact-source-holder.yaml`, wait for Ready, then run one source
canary:

```console
"$DD_LANE/dynamo/run_provisioned_trial.sh" \
  --run-id dd-direct-smoke --evidence-root "$DD_EVIDENCE" \
  --node "$TARGET_NODE" --kubeconfig "$DD_KUBECONFIG" \
  --artifact-holder diffdock-native-f7-holder-hf93 \
  --checkpoint-id diffdock-native-f7-v1 --target-glibc-version 2.35 \
  --artifact-manifest-sha256 b1c477efdfc6bcb8e253462524cef24fef6e059f43c97a1fcb94b85dca81e0b8 \
  --cleanup
```

Delete only that source holder, create `artifact-buffered-variant.yaml`, and
require its Pod to finish Succeeded. Its write-once receipt must report
manifest `93a83188fb0adcc89c1278f136595c6dbce1b3fe9c412c3ccf65f704745ec1fe`,
121 hardlinked payload files, the preserved rootfs delta, and buffered image
I/O. Create `artifact-holder.yaml`; Ready means all 7,516,058,314 bytes of the
buffered artifact were verified and read through the page cache.

## Buffered n=3

```console
for DD_RUN in dd-buf-p1 dd-buf-p2 dd-buf-p3; do
  "$DD_LANE/dynamo/run_provisioned_trial.sh" \
    --run-id "$DD_RUN" --evidence-root "$DD_EVIDENCE" \
    --node "$TARGET_NODE" --kubeconfig "$DD_KUBECONFIG" \
    --artifact-holder diffdock-native-f7-holder-hf93 \
    --checkpoint-id diffdock-native-f7-v3-buffered \
    --target-glibc-version 2.35 \
    --artifact-manifest-sha256 93a83188fb0adcc89c1278f136595c6dbce1b3fe9c412c3ccf65f704745ec1fe \
    --cleanup
done
```

Accept only three PASS receipts, exactly two semantic requests and two passes
per run, and no surviving run-scoped target/probe/worker objects. Retain the
DiffDock artifact PVC, cache PVC, PodSnapshotContent, and buffered holder.
The completed exact results are in `results.json`.
