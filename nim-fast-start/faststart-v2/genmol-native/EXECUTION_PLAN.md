# GenMol native capture and qualification procedure

This procedure was executed on 2026-08-18 with the pinned d5ce worker and the
explicit performance-validation acknowledgement shown below. `results.json`
is the compact qualification record. The exact Jammy CUDA base still needs a
baseline SBOM, so this is performance evidence rather than a full agent
compliance release.

Use one shell for the complete procedure so `set -Eeuo pipefail` makes every
failed identity or semantic check terminal.

## 1. Acknowledge the performance worker and establish the boundary

Do not mutate the release fields. The explicit CLI flag authorizes only the
exact pinned `performance-validation-only` contract and retains
`release_ready: false` in every receipt.

```console
set -Eeuo pipefail
umask 077

: "${FASTSTART_V2_ROOT:?set FASTSTART_V2_ROOT}"
: "${FASTSTART_EVIDENCE_ROOT:?set FASTSTART_EVIDENCE_ROOT}"
: "${KUBECONFIG:?set KUBECONFIG}"
: "${EXPECTED_API_SERVER:?set EXPECTED_API_SERVER}"
: "${TARGET_NODE:?set TARGET_NODE}"
export GENMOL_LANE=${FASTSTART_V2_ROOT}/genmol-native
export GENMOL_KUBECONFIG=${KUBECONFIG}
export GENMOL_NODE=${TARGET_NODE}
export GENMOL_EVIDENCE=${FASTSTART_EVIDENCE_ROOT}/genmol-native-f7-$(date -u +%Y%m%dT%H%M%SZ)
install -d -m 0700 "$GENMOL_EVIDENCE" "$GENMOL_EVIDENCE/runs"

jq -e '
  .approved == true and
  .release_ready == false and
  (.release_blocker | type == "string" and length > 0) and
  .worker_classification == "performance-validation-only" and
  (.supported_image_io_modes | index("direct")) != null and
  (.supported_image_io_modes | index("buffered")) != null and
  (.worker_image | test("@sha256:[0-9a-f]{64}$")) and
  (.worker_executable_sha256 | test("^[0-9a-f]{64}$")) and
  (.tool_bundle.content_sha256 | test("^[0-9a-f]{64}$")) and
  (.tool_bundle.glibc_compatibility_sha256 | test("^[0-9a-f]{64}$")) and
  .tool_bundle.maximum_required_glibc == "2.35"
' "$GENMOL_LANE/dynamo/restore-interface.live.json" >/dev/null
python3 "$GENMOL_LANE/render_snapshot_agent.py" \
  --contract "$GENMOL_LANE/dynamo/restore-interface.live.json" \
  --allow-performance-validation-worker \
  > "$GENMOL_EVIDENCE/snapshot-agent.yaml"
install -m 0600 "$GENMOL_LANE/dynamo/restore-interface.live.json" \
  "$GENMOL_EVIDENCE/restore-interface.json"
sha256sum "$GENMOL_EVIDENCE/restore-interface.json" \
  > "$GENMOL_EVIDENCE/restore-interface.sha256"

GENMOL_SERVER=$(kubectl --kubeconfig "$GENMOL_KUBECONFIG" config view --minify \
  -o jsonpath='{.clusters[0].cluster.server}')
test "$GENMOL_SERVER" = "$EXPECTED_API_SERVER"
kubectl --kubeconfig "$GENMOL_KUBECONFIG" get node "$GENMOL_NODE" -o json \
  > "$GENMOL_EVIDENCE/node-before.json"
jq -e --arg node "$GENMOL_NODE" '
  .metadata.name == $node and
  any(.status.conditions[]?; .type == "Ready" and .status == "True") and
  ((.status.allocatable["nvidia.com/gpu"] // "0") | tonumber) == 1 and
  .metadata.labels["nebius.com/gpu-name"] == "H100" and
  .metadata.labels["node.kubernetes.io/instance-type"] == "gpu-h100-sxm"
' "$GENMOL_EVIDENCE/node-before.json" >/dev/null

kubectl --kubeconfig "$GENMOL_KUBECONFIG" get pods -A \
  --field-selector "spec.nodeName=$GENMOL_NODE" -o json \
  > "$GENMOL_EVIDENCE/node-pods-before.json"
jq -e '
  [
    .items[]
    | select(.status.phase == "Pending" or .status.phase == "Running")
    | select(any((((.spec.initContainers // []) + (.spec.containers // []))[]);
        ((.resources.requests["nvidia.com/gpu"] // "0") | tonumber) > 0))
  ] | length == 0
' "$GENMOL_EVIDENCE/node-pods-before.json" >/dev/null
```

Stop if the node is not the exact Ready H100 or another active Pod requests its
GPU. Do not delete or modify another lane's objects to make this check pass.

## 2. Create isolated storage and the capture agent

The shared snapshot ServiceAccount and ConfigMaps must already exist. Preserve
their live identities before creating anything:

```console
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start get serviceaccount \
  archvteams-2407-native-snapshot -o json \
  > "$GENMOL_EVIDENCE/snapshot-serviceaccount.json"
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start get configmap \
  archvteams-2407-native-snapshot-config -o json \
  > "$GENMOL_EVIDENCE/snapshot-config.json"
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start get configmap \
  archvteams-2407-native-snapshot-seccomp -o json \
  > "$GENMOL_EVIDENCE/snapshot-seccomp.json"

kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start create \
  -f "$GENMOL_LANE/storage.yaml"
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start create \
  -f "$GENMOL_EVIDENCE/snapshot-agent.yaml"
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start wait \
  --for=jsonpath='{.status.phase}'=Bound pvc/genmol-native-f7-artifacts \
  --timeout=600s
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start wait \
  --for=condition=Ready pod/genmol-native-f7-snapshot-agent-t12 \
  --timeout=300s
```

All mutations use `create`; an unexpected name collision stops the run.

## 3. Start and semantically qualify the donor

Build the ConfigMap directly from the two digest-pinned local files. The donor
copies both values to regular emptyDir files and rehashes them before use.

```console
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start create configmap \
  genmol-native-f7-validator-r1 \
  --from-file=VALIDATOR_SOURCE="$GENMOL_LANE/validate_genmol.py" \
  --from-file=REQUEST_JSON="$GENMOL_LANE/fixtures/requests-qed-logp.json" \
  --dry-run=client -o yaml > "$GENMOL_EVIDENCE/donor-configmap.yaml"
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start create \
  -f "$GENMOL_EVIDENCE/donor-configmap.yaml"
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start create \
  -f "$GENMOL_LANE/donor-job.yaml"
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start wait \
  --for=jsonpath='{.status.phase}'=Bound pvc/genmol-native-f7-cache \
  --timeout=600s

GENMOL_DONOR_POD=$(kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start get pods \
  -l job-name=genmol-native-f7-donor-r1 -o json | \
  jq -er 'select((.items | length) == 1) | .items[0].metadata.name')
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start wait \
  --for=condition=Ready "pod/$GENMOL_DONOR_POD" --timeout=3600s
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start get pod \
  "$GENMOL_DONOR_POD" -o json > "$GENMOL_EVIDENCE/donor-pod.json"
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start logs \
  "$GENMOL_DONOR_POD" > "$GENMOL_EVIDENCE/donor.log"
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start exec \
  "$GENMOL_DONOR_POD" -- cat /snapshot-control/target-runtime.json \
  > "$GENMOL_EVIDENCE/target-runtime.json"

jq -e \
  --arg image 'nvcr.io/nim/nvidia/genmol@sha256:139b909a450fe1fb81198214784a15f67e172e766a93a1569827ba5aa05b4541' \
  --arg node "$GENMOL_NODE" '
    .schema == "archvteams.nebius.ai/target-runtime/v1" and
    .image == $image and .node == $node and
    (.glibc_version | test("^[0-9]+\\.[0-9]+$"))
  ' "$GENMOL_EVIDENCE/target-runtime.json" >/dev/null
GENMOL_TARGET_GLIBC=$(jq -er '.glibc_version' "$GENMOL_EVIDENCE/target-runtime.json")
```

Ready means the exact image completed the frozen QED and LogP loopback POSTs,
including both RDKit descriptor checks. The target glibc is measured from that
image, never guessed.

## 4. Capture the UID-bound direct artifact

```console
python3 "$GENMOL_LANE/render_capture.py" \
  --donor-pod-json "$GENMOL_EVIDENCE/donor-pod.json" \
  > "$GENMOL_EVIDENCE/podsnapshotcontent.yaml"
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start create \
  -f "$GENMOL_EVIDENCE/podsnapshotcontent.yaml"
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start wait \
  --for=condition=Ready \
  podsnapshotcontent/genmol-native-f7-v1-direct-t12 --timeout=1800s
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start get \
  podsnapshotcontent genmol-native-f7-v1-direct-t12 -o json \
  > "$GENMOL_EVIDENCE/podsnapshotcontent.json"
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start logs \
  genmol-native-f7-snapshot-agent-t12 \
  > "$GENMOL_EVIDENCE/capture-agent.log"
```

Preserve the evidence above before freeing the GPU, then delete only the three
exact capture-stage objects:

```console
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start delete job \
  genmol-native-f7-donor-r1 --wait=true --timeout=300s
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start delete pod \
  genmol-native-f7-snapshot-agent-t12 --wait=true --timeout=300s
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start delete configmap \
  genmol-native-f7-validator-r1 --wait=true --timeout=120s
```

## 5. Verify and prewarm the direct artifact

The CPU-only holder hashes every regular file with four readers, verifies the
direct manifest identity, and becomes Ready only after reading the complete
artifact.

```console
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start create \
  -f "$GENMOL_LANE/artifact-holder.yaml"
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start wait \
  --for=condition=Ready pod/genmol-native-f7-holder-t12-v2 --timeout=1800s
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start logs \
  genmol-native-f7-holder-t12-v2 | tail -n 1 \
  > "$GENMOL_EVIDENCE/artifact-direct-receipt.json"
GENMOL_DIRECT_MANIFEST=$(jq -er '
  select(.schema == "archvteams.nebius.ai/genmol-native-artifact-receipt/v1") |
  select(.checkpoint_id == "genmol-native-f7-v1") |
  select(.image_io_mode == "direct") |
  select(.prewarm_bytes == .regular_file_bytes) |
  .manifest_sha256
' "$GENMOL_EVIDENCE/artifact-direct-receipt.json")
test "${#GENMOL_DIRECT_MANIFEST}" -eq 64
```

## 6. Run direct-mode `n=3`

The runner timestamps demand before creating the target. It binds the live Pod
UID, container ID, cgroup, Pod IP, image ID, and canonical PodSpec hash. It then
submits the CPU semantic probe before the one-shot restore worker. Each of the
three trials must finish two distinct ClusterIP calls; `--cleanup` removes only
that run's target, worker, probe, Service, and binding objects.

```console
"$GENMOL_LANE/dynamo/run_n3.sh" \
  --run-prefix genmol-direct-v2 \
  --evidence-root "$GENMOL_EVIDENCE" \
  --node "$GENMOL_NODE" \
  --kubeconfig "$GENMOL_KUBECONFIG" \
  --artifact-holder genmol-native-f7-holder-t12-v2 \
  --checkpoint-id genmol-native-f7-v1 \
  --target-glibc-version "$GENMOL_TARGET_GLIBC" \
  --image-io-mode direct \
  --artifact-manifest-sha256 "$GENMOL_DIRECT_MANIFEST" \
  --allow-performance-validation-worker \
  --cleanup
```

Stop on the first failed trial and retain its private run directory. A direct
PASS is three independent restores and six strict semantic responses.

## 7. Build, verify, and smoke the buffered artifact

The builder accepts only the exact full-prewarm direct receipt. It refuses an
existing destination, hard-links every immutable payload file, writes a new
manifest inode with the new checkpoint identity and buffered image-I/O mode,
then atomically publishes the destination directory.

```console
python3 "$GENMOL_LANE/render_buffered_variant.py" \
  --artifact-receipt "$GENMOL_EVIDENCE/artifact-direct-receipt.json" \
  > "$GENMOL_EVIDENCE/buffered-build-v2.yaml"
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start create \
  -f "$GENMOL_EVIDENCE/buffered-build-v2.yaml"
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start wait \
  --for=condition=Complete job/genmol-native-f7-v2-buffered-build-v2 \
  --timeout=900s
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start logs \
  job/genmol-native-f7-v2-buffered-build-v2 | tail -n 1 \
  > "$GENMOL_EVIDENCE/buffered-build-receipt.json"
jq -e '
  .schema == "archvteams.nebius.ai/genmol-buffered-build/v1" and
  .status == "PASS" and
  .checkpoint_id == "genmol-native-f7-v2-buffered" and
  .image_io_mode == "buffered" and
  (.manifest_sha256 | test("^[0-9a-f]{64}$"))
' "$GENMOL_EVIDENCE/buffered-build-receipt.json" >/dev/null

kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start create \
  -f "$GENMOL_LANE/artifact-holder-buffered.yaml"
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start wait \
  --for=condition=Ready pod/genmol-native-f7-buffered-holder-t12 \
  --timeout=1800s
kubectl --kubeconfig "$GENMOL_KUBECONFIG" -n nim-fast-start logs \
  genmol-native-f7-buffered-holder-t12 | tail -n 1 \
  > "$GENMOL_EVIDENCE/artifact-buffered-receipt.json"
GENMOL_BUFFERED_MANIFEST=$(jq -er '
  select(.schema == "archvteams.nebius.ai/genmol-native-artifact-receipt/v1") |
  select(.checkpoint_id == "genmol-native-f7-v2-buffered") |
  select(.image_io_mode == "buffered") |
  select(.prewarm_bytes == .regular_file_bytes) |
  .manifest_sha256
' "$GENMOL_EVIDENCE/artifact-buffered-receipt.json")
test "$GENMOL_BUFFERED_MANIFEST" = \
  "$(jq -er '.manifest_sha256' "$GENMOL_EVIDENCE/buffered-build-receipt.json")"

"$GENMOL_LANE/dynamo/run_provisioned_trial.sh" \
  --run-id genmol-buf-smoke-v2 \
  --evidence-root "$GENMOL_EVIDENCE" \
  --node "$GENMOL_NODE" \
  --kubeconfig "$GENMOL_KUBECONFIG" \
  --artifact-holder genmol-native-f7-buffered-holder-t12 \
  --checkpoint-id genmol-native-f7-v2-buffered \
  --target-glibc-version "$GENMOL_TARGET_GLIBC" \
  --image-io-mode buffered \
  --artifact-manifest-sha256 "$GENMOL_BUFFERED_MANIFEST" \
  --allow-performance-validation-worker \
  --cleanup
```

The buffered variant remains a candidate until this complete two-call smoke
reports `PASS`.

## 8. Run buffered-mode `n=3` and compare

```console
"$GENMOL_LANE/dynamo/run_n3.sh" \
  --run-prefix genmol-buffered-v2 \
  --evidence-root "$GENMOL_EVIDENCE" \
  --node "$GENMOL_NODE" \
  --kubeconfig "$GENMOL_KUBECONFIG" \
  --artifact-holder genmol-native-f7-buffered-holder-t12 \
  --checkpoint-id genmol-native-f7-v2-buffered \
  --target-glibc-version "$GENMOL_TARGET_GLIBC" \
  --image-io-mode buffered \
  --artifact-manifest-sha256 "$GENMOL_BUFFERED_MANIFEST" \
  --allow-performance-validation-worker \
  --cleanup

jq -s '
  map({
    checkpoint_id,
    image_io_mode,
    trial_count,
    request_count,
    demand_to_http_ready_seconds,
    demand_to_kubernetes_ready_seconds,
    semantic_request_1_seconds,
    semantic_request_2_seconds,
    demand_to_two_semantic_seconds,
    worker_restore_seconds,
    statistics_seconds
  })
' "$GENMOL_EVIDENCE/n3-genmol-direct-v2-direct.json" \
  "$GENMOL_EVIDENCE/n3-genmol-buffered-v2-buffered.json" \
  > "$GENMOL_EVIDENCE/direct-vs-buffered.json"
```

Report all HTTP-readiness, call-1, call-2, and T0-through-call-2 values and
medians. Report Kubernetes Pod Ready and worker restore only as separate
diagnostics, declare the storage state, and keep full prewarm outside T0. Do not
report the retained 4.831-second page-cache experiment as the
production-shaped result.

## 9. Cleanup after evidence is durable

The runner already deletes its exact run-scoped objects when `--cleanup` is
used. After copying all receipts, remove only the direct holder and completed
buffered-builder Job. Retain the selected buffered holder, PodSnapshotContent,
and both PVCs until the qualification result and rollback decision are
accepted.

Deleting either PVC is destructive and intentionally has no command in this
plan.
