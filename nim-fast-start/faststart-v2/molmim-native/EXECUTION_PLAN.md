# Executed MolMIM performance qualification

This plan was executed on 2026-08-18 against the exact allowed cluster and t12
H100. Evidence is retained at
`<private-evidence-root>/molmim-native-f7-20260818T073602Z`.
Buffered native won the strict response-boundary comparison: 15.431630 seconds
median versus 24.147146 seconds conventional cached, 36.0934% faster. The
commands below remain the reproducible runbook. Use one shell so an identity,
scheduling, semantic, or cleanup error is terminal. Never delete another
lane's objects to make capacity available.

## 1. Establish the exact boundary

```console
set -Eeuo pipefail
umask 077

: "${FASTSTART_V2_ROOT:?set FASTSTART_V2_ROOT}"
: "${FASTSTART_EVIDENCE_ROOT:?set FASTSTART_EVIDENCE_ROOT}"
: "${KUBECONFIG:?set KUBECONFIG}"
: "${EXPECTED_API_SERVER:?set EXPECTED_API_SERVER}"
: "${TARGET_NODE:?set TARGET_NODE}"
export MM_LANE=${FASTSTART_V2_ROOT}/molmim-native
export MM_KUBECONFIG=${KUBECONFIG}
export MM_NODE=${TARGET_NODE}
export MM_EVIDENCE=${FASTSTART_EVIDENCE_ROOT}/molmim-native-f7-$(date -u +%Y%m%dT%H%M%SZ)
install -d -m 0700 "$MM_EVIDENCE" "$MM_EVIDENCE/runs"

test "$(sha256sum "$MM_LANE/validate_molmim.py" | cut -d' ' -f1)" = \
  0d87fd53b554a629b8fb83c5abc79b074220f223ea97f7c1d8802d48e4833bd7
test "$(sha256sum "$MM_LANE/fixtures/request-cmaes-qed.json" | cut -d' ' -f1)" = \
  053e8a5befb020695e4d27200d21b296e7171f480075125cfa6f7b5a71dbc42d

MM_SERVER=$(kubectl --kubeconfig "$MM_KUBECONFIG" config view --minify \
  -o jsonpath='{.clusters[0].cluster.server}')
test "$MM_SERVER" = "$EXPECTED_API_SERVER"
kubectl --kubeconfig "$MM_KUBECONFIG" get node "$MM_NODE" -o json \
  > "$MM_EVIDENCE/node-before.json"
jq -e --arg node "$MM_NODE" '
  .metadata.name == $node and
  any(.status.conditions[]?; .type == "Ready" and .status == "True") and
  ((.status.allocatable["nvidia.com/gpu"] // "0") | tonumber) >= 1 and
  ((.metadata.labels["nvidia.com/gpu.product"] // "") | test("H100"; "i"))
' "$MM_EVIDENCE/node-before.json" >/dev/null

kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start get pods \
  --field-selector "spec.nodeName=$MM_NODE" -o json \
  > "$MM_EVIDENCE/node-pods-before.json"
jq -e '
  [
    .items[]
    | select(.status.phase == "Pending" or .status.phase == "Running")
    | select(any(.spec.containers[]?;
        ((.resources.requests["nvidia.com/gpu"] // "0") | tonumber) > 0))
  ] | length == 0
' "$MM_EVIDENCE/node-pods-before.json" >/dev/null
```

Stop if this is not the exact Ready H100 or any active Pod requests its GPU.

## 2. Materialize and prewarm the retained cache

All resources are created under new, exact names. A collision is an error.

The live run could not attach another volume to hf93. It therefore streamed the
exact 284,497,920-byte source through a private local evidence file, verified
its tree hash, populated the already-attached t12 claim, and verified the same
tree there. The direct `cache-seed-job.yaml` sequence below remains valid only
when hf93 has attachment capacity.

```console
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start create \
  -f "$MM_LANE/storage.yaml"
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start wait \
  --for=jsonpath='{.status.phase}'=Bound \
  pvc/molmim-native-f7-artifacts pvc/molmim-native-f7-cache --timeout=600s

kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start create \
  -f "$MM_LANE/cache-seed-job.yaml"
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start wait \
  --for=condition=Complete job/molmim-native-f7-cache-seed-r1 --timeout=900s
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start logs \
  job/molmim-native-f7-cache-seed-r1 | tail -n 1 \
  > "$MM_EVIDENCE/cache-seed-receipt.json"
jq -e '
  .schema == "archvteams.nebius.ai/molmim-cache-receipt/v1" and
  .source_host_path == "/snapshots/nim-caches/molmim" and
  .checkpoint_file_bytes == 281589760 and
  .prewarm_bytes == .regular_file_bytes and
  .status == "PASS" and
  (.tree_sha256 | test("^[0-9a-f]{64}$"))
' "$MM_EVIDENCE/cache-seed-receipt.json" >/dev/null

kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start create \
  -f "$MM_LANE/cache-holder.yaml"
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start wait \
  --for=condition=Ready pod/molmim-native-f7-cache-holder-t12 --timeout=900s
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start logs \
  molmim-native-f7-cache-holder-t12 | tail -n 1 \
  > "$MM_EVIDENCE/cache-holder-receipt.json"
test "$(jq -er .tree_sha256 "$MM_EVIDENCE/cache-holder-receipt.json")" = \
  "$(jq -er .tree_sha256 "$MM_EVIDENCE/cache-seed-receipt.json")"

kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start create \
  -f "$MM_LANE/conventional/image-cache-holder.yaml"
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start wait \
  --for=condition=Ready pod/molmim-native-f7-image-holder-t12 --timeout=1800s
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start get pod \
  molmim-native-f7-image-holder-t12 -o json \
  > "$MM_EVIDENCE/image-holder.json"
jq -e '
  (.status.containerStatuses | length) == 1 and
  (.status.containerStatuses[0].imageID |
    sub("^docker-pullable://"; "")) ==
    "nvcr.io/nim/nvidia/molmim@sha256:7700c5556935a93055bee5367d36acb6d3e55d22fd1ba28503f5447656fa63fa"
' "$MM_EVIDENCE/image-holder.json" >/dev/null
```

The image holder is setup, not measured time. It requests no GPU and ensures
every conventional trial is an exact-image cached start. Each counted target
must also have a `Pulled` Event stating the image was already present.

## 3. Measure the conventional cached control first

```console
"$MM_LANE/conventional/run_cached_n3.sh" \
  --run-prefix mm-rx \
  --evidence-root "$MM_EVIDENCE" \
  --node "$MM_NODE" \
  --kubeconfig "$MM_KUBECONFIG" \
  --cleanup
```

This produces
`$MM_EVIDENCE/n3-mm-rx-conventional-cached.json`. It requires three
scheduler-created real NIM starts, three cached-image Event proofs, and six
strict CMA-ES/QED responses. Do not continue if any trial fails.

## 4. Open the native performance-worker gate

The checked-in d5ce contract deliberately has `release_ready: false` and is
classified `performance-validation-only`. These measurements explicitly used
`--allow-performance-validation-worker`; omitting that flag still fails before
mutation. Replace the contract with a full compliance release before making a
release-readiness claim.

```console
jq -e '
  .approved == true and
  .release_ready == false and
  (.release_blocker | type == "string" and length > 0) and
  .worker_classification == "performance-validation-only" and
  (.supported_image_io_modes | index("direct")) != null and
  (.supported_image_io_modes | index("buffered")) != null and
  .worker_executable_sha256 ==
    "941157dd1815acf6f3e26cbe9dea65ee1c9a398c719881d474e5d7c5c7e28651" and
  .tool_bundle.content_sha256 ==
    "c0d638100c03fa35973e82859d15b9c8dd1bcbf0fe9cb185b58cc21fae7ead1e" and
  .tool_bundle.maximum_required_glibc == "2.35"
' "$MM_LANE/dynamo/restore-interface.live.json" >/dev/null

python3 "$MM_LANE/render_snapshot_agent.py" \
  --contract "$MM_LANE/dynamo/restore-interface.live.json" \
  > "$MM_EVIDENCE/snapshot-agent.yaml"
install -m 0600 "$MM_LANE/dynamo/restore-interface.live.json" \
  "$MM_EVIDENCE/restore-interface.json"

kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start get serviceaccount \
  archvteams-2407-native-snapshot -o json \
  > "$MM_EVIDENCE/snapshot-serviceaccount.json"
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start get configmap \
  archvteams-2407-native-snapshot-config -o json \
  > "$MM_EVIDENCE/snapshot-config.json"
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start get configmap \
  archvteams-2407-native-snapshot-seccomp -o json \
  > "$MM_EVIDENCE/snapshot-seccomp.json"
python3 - "$MM_EVIDENCE" <<'PY'
import hashlib
import json
import pathlib
import sys
import uuid

root = pathlib.Path(sys.argv[1])
service_account = json.loads((root / "snapshot-serviceaccount.json").read_text())
config = json.loads((root / "snapshot-config.json").read_text())
seccomp = json.loads((root / "snapshot-seccomp.json").read_text())

def require(condition, message):
    if not condition:
        raise SystemExit(message)

expected = (
    (
        service_account,
        "ServiceAccount",
        "archvteams-2407-native-snapshot",
        None,
        None,
    ),
    (
        config,
        "ConfigMap",
        "archvteams-2407-native-snapshot-config",
        "config.yaml",
        "e1eeddafb76c80cf19b78dd17cf524da331d8d9a18df235108d58087ab6f9ccf",
    ),
    (
        seccomp,
        "ConfigMap",
        "archvteams-2407-native-snapshot-seccomp",
        "block-iouring.json",
        "ebbe5e221b6b331bb84efbdfea7adb88e9dddab62a2ea901598bad09fe7f76a0",
    ),
)
for value, kind, name, data_key, digest in expected:
    metadata = value.get("metadata", {})
    require(
        value.get("apiVersion") == "v1" and value.get("kind") == kind,
        f"wrong API identity for {name}",
    )
    require(metadata.get("name") == name, f"wrong object name for {name}")
    require(metadata.get("namespace") == "nim-fast-start", f"wrong namespace for {name}")
    require(metadata.get("deletionTimestamp") is None, f"{name} is deleting")
    require(str(uuid.UUID(metadata["uid"])) == metadata["uid"], f"invalid UID for {name}")
    require(isinstance(metadata.get("resourceVersion"), str), f"missing version for {name}")
    if data_key is not None:
        require(set(value.get("data", {})) == {data_key}, f"wrong data keys for {name}")
        payload = value["data"][data_key].encode("utf-8")
        require(hashlib.sha256(payload).hexdigest() == digest, f"wrong content for {name}")
PY
```

The current `d5ce1ea...` image opens only the explicitly acknowledged
performance gate; the missing exact-base compliance baseline remains outside
this timing qualification.

## 5. Capture one UID-bound direct artifact

```console
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start create \
  -f "$MM_EVIDENCE/snapshot-agent.yaml"
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start wait \
  --for=condition=Ready pod/molmim-native-f7-snapshot-agent-t12 --timeout=300s

kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start create configmap \
  molmim-native-f7-validator-r1 \
  --from-file=VALIDATOR_SOURCE="$MM_LANE/validate_molmim.py" \
  --from-file=REQUEST_JSON="$MM_LANE/fixtures/request-cmaes-qed.json" \
  --dry-run=client -o yaml > "$MM_EVIDENCE/donor-configmap.yaml"
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start create \
  -f "$MM_EVIDENCE/donor-configmap.yaml"
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start create \
  -f "$MM_LANE/donor-job.yaml"

MM_DONOR=$(kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start get pods \
  -l job-name=molmim-native-f7-donor-r1 -o json | \
  jq -er 'select((.items | length) == 1) | .items[0].metadata.name')
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start wait \
  --for=condition=Ready "pod/$MM_DONOR" --timeout=1800s
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start get pod \
  "$MM_DONOR" -o json > "$MM_EVIDENCE/donor-pod.json"
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start get job \
  molmim-native-f7-donor-r1 -o json > "$MM_EVIDENCE/donor-job.json"
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start exec "$MM_DONOR" -- \
  cat /snapshot-control/target-runtime.json > "$MM_EVIDENCE/target-runtime.json"

jq -e --arg image \
  'nvcr.io/nim/nvidia/molmim@sha256:7700c5556935a93055bee5367d36acb6d3e55d22fd1ba28503f5447656fa63fa' \
  --arg node "$MM_NODE" '
    .schema == "archvteams.nebius.ai/target-runtime/v1" and
    .image == $image and .node == $node and
    (.glibc_version | test("^[0-9]+\\.[0-9]+$"))
  ' "$MM_EVIDENCE/target-runtime.json" >/dev/null
MM_GLIBC=$(jq -er .glibc_version "$MM_EVIDENCE/target-runtime.json")

python3 "$MM_LANE/render_capture.py" \
  --donor-pod-json "$MM_EVIDENCE/donor-pod.json" \
  --donor-job-json "$MM_EVIDENCE/donor-job.json" \
  > "$MM_EVIDENCE/podsnapshotcontent.yaml"
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start create \
  -f "$MM_EVIDENCE/podsnapshotcontent.yaml"
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start wait \
  --for=condition=Ready \
  podsnapshotcontent/molmim-native-f7-v1-direct-t12 --timeout=1800s
```

The donor becomes Ready only after starting from the fully prewarmed cache,
using the exact NIM-required writable-cache and NGC bootstrap contract,
`TORCHINDUCTOR_COMPILE_THREADS=1`, and passing both strict loopback calls. The
capture renderer binds the captured Job UID and exact rendered PodSpec as well
as the Pod name, UID, image, container, node, and Ready state. Any artifact made
from a donor that received `NGC_API_KEY` is credential-bearing and must be
discarded and recreated after rotating that credential.

After preserving donor and capture logs, delete only the donor Job, its
validator ConfigMap, and the snapshot-agent Pod to release the GPU.

## 6. Verify direct, materialize buffered, and prewarm both

```console
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start create \
  -f "$MM_LANE/artifact-holder.yaml"
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start wait \
  --for=condition=Ready pod/molmim-native-f7-holder-t12 --timeout=1800s
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start logs \
  molmim-native-f7-holder-t12 | tail -n 1 \
  > "$MM_EVIDENCE/artifact-direct-receipt.json"
MM_DIRECT_MANIFEST=$(jq -er '
  select(.schema == "archvteams.nebius.ai/molmim-native-artifact-receipt/v1") |
  select(.checkpoint_id == "molmim-native-f7-v1") |
  select(.image_io_mode == "direct") |
  select(.prewarm_bytes == .regular_file_bytes) |
  .manifest_sha256
' "$MM_EVIDENCE/artifact-direct-receipt.json")

python3 "$MM_LANE/render_buffered_variant.py" \
  --artifact-receipt "$MM_EVIDENCE/artifact-direct-receipt.json" \
  > "$MM_EVIDENCE/buffered-build.yaml"
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start create \
  -f "$MM_EVIDENCE/buffered-build.yaml"
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start wait \
  --for=condition=Complete job/molmim-native-f7-v2-buffered-build --timeout=900s
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start logs \
  job/molmim-native-f7-v2-buffered-build | tail -n 1 \
  > "$MM_EVIDENCE/buffered-build-receipt.json"
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start create \
  -f "$MM_LANE/artifact-holder-buffered.yaml"
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start wait \
  --for=condition=Ready pod/molmim-native-f7-buffered-holder-t12 --timeout=1800s
kubectl --kubeconfig "$MM_KUBECONFIG" -n nim-fast-start logs \
  molmim-native-f7-buffered-holder-t12 | tail -n 1 \
  > "$MM_EVIDENCE/artifact-buffered-receipt.json"
MM_BUFFERED_MANIFEST=$(jq -er '
  select(.schema == "archvteams.nebius.ai/molmim-native-artifact-receipt/v1") |
  select(.checkpoint_id == "molmim-native-f7-v2-buffered") |
  select(.image_io_mode == "buffered") |
  select(.prewarm_bytes == .regular_file_bytes) |
  .manifest_sha256
' "$MM_EVIDENCE/artifact-buffered-receipt.json")
test "$MM_BUFFERED_MANIFEST" = \
  "$(jq -er .manifest_sha256 "$MM_EVIDENCE/buffered-build-receipt.json")"
```

The buffered builder accepts only the full-prewarm direct receipt, refuses an
existing destination, hard-links immutable payload files, and writes a new
manifest inode with the buffered mode. This is the generic worker's actual
legacy buffered path, not a writeback alias.

## 7. Run the direct canary and buffered native n=3

```console
"$MM_LANE/dynamo/run_provisioned_trial.sh" \
  --run-id mm-direct-canary3 \
  --evidence-root "$MM_EVIDENCE" \
  --node "$MM_NODE" \
  --kubeconfig "$MM_KUBECONFIG" \
  --artifact-holder molmim-native-f7-holder-t12 \
  --checkpoint-id molmim-native-f7-v1 \
  --target-glibc-version "$MM_GLIBC" \
  --image-io-mode direct \
  --artifact-manifest-sha256 "$MM_DIRECT_MANIFEST" \
  --allow-performance-validation-worker \
  --cleanup

"$MM_LANE/dynamo/run_n3.sh" \
  --run-prefix mm-bufrx \
  --evidence-root "$MM_EVIDENCE" \
  --node "$MM_NODE" \
  --kubeconfig "$MM_KUBECONFIG" \
  --artifact-holder molmim-native-f7-buffered-holder-t12 \
  --checkpoint-id molmim-native-f7-v2-buffered \
  --target-glibc-version "$MM_GLIBC" \
  --image-io-mode buffered \
  --artifact-manifest-sha256 "$MM_BUFFERED_MANIFEST" \
  --allow-performance-validation-worker \
  --cleanup
```

Each native trial starts demand before creating a scheduler-bound inert GPU
target, binds its live UID/container/cgroup/IP/image/PodSpec, creates the CPU
probe before the restore worker, and requires two strict ClusterIP calls.

## 8. Fail fast on the production comparison

```console
set +e
python3 "$MM_LANE/conventional/compare.py" \
  --conventional "$MM_EVIDENCE/n3-mm-rx-conventional-cached.json" \
  --buffered-native "$MM_EVIDENCE/n3-mm-bufrx-buffered.json" \
  > "$MM_EVIDENCE/startup-decision-response-boundary.json.partial"
MM_DECISION_STATUS=$?
set -e
mv "$MM_EVIDENCE/startup-decision-response-boundary.json.partial" \
  "$MM_EVIDENCE/startup-decision-response-boundary.json"

if [[ $MM_DECISION_STATUS -eq 3 ]]; then
  jq . "$MM_EVIDENCE/startup-decision-response-boundary.json"
  echo 'Buffered native did not beat conventional cached; stop MolMIM restore work.'
  exit 3
fi
test "$MM_DECISION_STATUS" -eq 0
```

The live status was `PASS` with
`PROMOTE_BUFFERED_NATIVE_FOR_FURTHER_QUALIFICATION`: buffered native was
8.715516 seconds faster at the median.

Direct-mode n=3 is optional attribution work; the required direct canary was
already slower and the requested selection is fully determined:

```console
"$MM_LANE/dynamo/run_n3.sh" \
  --run-prefix mm-direct \
  --evidence-root "$MM_EVIDENCE" \
  --node "$MM_NODE" \
  --kubeconfig "$MM_KUBECONFIG" \
  --artifact-holder molmim-native-f7-holder-t12 \
  --checkpoint-id molmim-native-f7-v1 \
  --target-glibc-version "$MM_GLIBC" \
  --image-io-mode direct \
  --artifact-manifest-sha256 "$MM_DIRECT_MANIFEST" \
  --allow-performance-validation-worker \
  --cleanup
```

## 9. Final ownership-scoped cleanup

Preserve every JSON/log receipt first. Delete only exact run-scoped MolMIM
targets, workers, probes, donor, and snapshot-agent resources, with UID
preconditions if any name was observed before this run. The live result retains
the selected buffered holder, cache/image holders, both PVCs, and direct
PodSnapshotContent; none request a GPU. Re-run the node Pod inventory and
require zero remaining MolMIM GPU requests. Do not remove shared snapshot
ServiceAccounts, ConfigMaps, storage classes, or another model's objects.
