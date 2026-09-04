# MSA Search native recapture and qualification plan

The 2026-08-18 live lane selected the cached conventional route recorded in
`results.json`. Checkpoint `msa-search-native-f7-v1` is non-promotable: its
donor used an `emptyDir` workspace, so it cannot be qualified against the final
cache-PVC topology. Do not retry, buffer, or promote v1. The commands below are
the retained procedure for a **fresh checkpoint ID** after donor and target use
identical final mounts at `/opt/nim/.cache` and `/opt/nim/workspace`.
The literal v1 names below document the completed attempt and are retained for
auditability; they must be replaced consistently with new run-scoped names
before a recapture. Do not run this file verbatim against the retained v1
objects.

The integrated portable-plus-buffered worker pinned in
`dynamo/restore-interface.live.json` remains performance-validation-only. The
normal release gate stays closed; an explicitly acknowledged performance run
uses `--allow-performance-validation-worker` and cannot establish release
approval.

Use one shell for the complete procedure so `set -Eeuo pipefail` makes every
failed identity or semantic check terminal.

## 1. Open the release gate and establish the boundary

Update these fields in the single contract input before starting:

- the final `worker_image`, `worker_executable_sha256`, and tool receipts;
- the matching materialized-source inputs and formal approval receipt;
- direct and buffered entries in `supported_image_io_modes`;
- `worker_classification` to `full-agent-compliance-release`;
- `release_blocker` to the empty string; and
- `release_ready` to `true`.

Do not open the gate on the performance-validation-only image. Then run:

```console
set -Eeuo pipefail
umask 077

: "${FASTSTART_V2_ROOT:?set FASTSTART_V2_ROOT}"
: "${FASTSTART_EVIDENCE_ROOT:?set FASTSTART_EVIDENCE_ROOT}"
: "${KUBECONFIG:?set KUBECONFIG}"
: "${EXPECTED_API_SERVER:?set EXPECTED_API_SERVER}"
: "${TARGET_NODE:?set TARGET_NODE}"
export MSA_LANE=${FASTSTART_V2_ROOT}/msa-search-native
export MSA_KUBECONFIG=${KUBECONFIG}
export MSA_NODE=${TARGET_NODE}
export MSA_EVIDENCE=${FASTSTART_EVIDENCE_ROOT}/msa-search-native-f7-$(date -u +%Y%m%dT%H%M%SZ)
install -d -m 0700 "$MSA_EVIDENCE" "$MSA_EVIDENCE/runs"

jq -e '
  .approved == true and
  .release_ready == true and
  .release_blocker == "" and
  .worker_classification == "full-agent-compliance-release" and
  (.supported_image_io_modes | index("direct")) != null and
  (.supported_image_io_modes | index("buffered")) != null and
  (.worker_image | test("@sha256:[0-9a-f]{64}$")) and
  (.worker_executable_sha256 | test("^[0-9a-f]{64}$")) and
  (.tool_bundle.content_sha256 | test("^[0-9a-f]{64}$")) and
  (.tool_bundle.glibc_compatibility_sha256 | test("^[0-9a-f]{64}$")) and
  .tool_bundle.maximum_required_glibc == "2.35"
' "$MSA_LANE/dynamo/restore-interface.live.json" >/dev/null
python3 "$MSA_LANE/render_snapshot_agent.py" \
  --contract "$MSA_LANE/dynamo/restore-interface.live.json" \
  > "$MSA_EVIDENCE/snapshot-agent.yaml"
install -m 0600 "$MSA_LANE/dynamo/restore-interface.live.json" \
  "$MSA_EVIDENCE/restore-interface.json"
sha256sum "$MSA_EVIDENCE/restore-interface.json" \
  > "$MSA_EVIDENCE/restore-interface.sha256"

MSA_SERVER=$(kubectl --kubeconfig "$MSA_KUBECONFIG" config view --minify \
  -o jsonpath='{.clusters[0].cluster.server}')
test "$MSA_SERVER" = "$EXPECTED_API_SERVER"
kubectl --kubeconfig "$MSA_KUBECONFIG" get node "$MSA_NODE" -o json \
  > "$MSA_EVIDENCE/node-before.json"
jq -e --arg node "$MSA_NODE" '
  .metadata.name == $node and
  any(.status.conditions[]?; .type == "Ready" and .status == "True") and
  ((.status.allocatable["nvidia.com/gpu"] // "0") | tonumber) >= 1 and
  ((.metadata.labels["nvidia.com/gpu.product"] // "") | test("H100"; "i"))
' "$MSA_EVIDENCE/node-before.json" >/dev/null

kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start get pods \
  --field-selector "spec.nodeName=$MSA_NODE" -o json \
  > "$MSA_EVIDENCE/node-pods-before.json"
jq -e '
  [
    .items[]
    | select(.status.phase == "Pending" or .status.phase == "Running")
    | select(any(.spec.containers[]?;
        ((.resources.requests["nvidia.com/gpu"] // "0") | tonumber) > 0))
  ] | length == 0
' "$MSA_EVIDENCE/node-pods-before.json" >/dev/null
```

Stop if the node is not the exact Ready H100 or another active Pod requests its
GPU. Do not delete or modify another lane's objects to make this check pass.

## 2. Create isolated storage and the release-gated capture agent

The shared snapshot ServiceAccount and ConfigMaps must already exist. Preserve
their live identities before creating anything:

```console
kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start get serviceaccount \
  archvteams-2407-native-snapshot -o json \
  > "$MSA_EVIDENCE/snapshot-serviceaccount.json"
kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start get configmap \
  archvteams-2407-native-snapshot-config -o json \
  > "$MSA_EVIDENCE/snapshot-config.json"
kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start get configmap \
  archvteams-2407-native-snapshot-seccomp -o json \
  > "$MSA_EVIDENCE/snapshot-seccomp.json"

kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start create \
  -f "$MSA_LANE/storage.yaml"
kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start wait \
  --for=jsonpath='{.status.phase}'=Bound pvc/msa-search-native-f7-artifacts \
  pvc/msa-search-native-f7-cache --timeout=600s
kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start create \
  -f "$MSA_EVIDENCE/snapshot-agent.yaml"
kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start wait \
  --for=condition=Ready pod/msa-search-native-f7-snapshot-agent-hf93 \
  --timeout=300s
```

All mutations use `create`; an unexpected name collision stops the run.

## 3. Start and semantically qualify the donor

Build the ConfigMap directly from the two digest-pinned local files. The donor
copies both values to regular emptyDir files and rehashes them before use.

```console
kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start create configmap \
  msa-search-native-f7-validator-r1 \
  --from-file=VALIDATOR_SOURCE="$MSA_LANE/validate_msa_search.py" \
  --from-file=REQUEST_JSON="$MSA_LANE/fixtures/request-pdb70.json" \
  --dry-run=client -o yaml > "$MSA_EVIDENCE/donor-configmap.yaml"
kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start create \
  -f "$MSA_EVIDENCE/donor-configmap.yaml"
kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start create \
  -f "$MSA_LANE/donor-job.yaml"

MSA_DONOR_POD=$(kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start get pods \
  -l job-name=msa-search-native-f7-donor-r1 -o json | \
  jq -er 'select((.items | length) == 1) | .items[0].metadata.name')
kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start wait \
  --for=condition=Ready "pod/$MSA_DONOR_POD" --timeout=3600s
kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start get pod \
  "$MSA_DONOR_POD" -o json > "$MSA_EVIDENCE/donor-pod.json"
kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start logs \
  "$MSA_DONOR_POD" > "$MSA_EVIDENCE/donor.log"
kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start exec \
  "$MSA_DONOR_POD" -- cat /snapshot-control/target-runtime.json \
  > "$MSA_EVIDENCE/target-runtime.json"

jq -e \
  --arg image 'nvcr.io/nim/colabfold/msa-search@sha256:944f3cf845761be8e42b33147ae08b68c61eca7cad67bf5251e1708d03c0165c' \
  --arg node "$MSA_NODE" '
    .schema == "archvteams.nebius.ai/target-runtime/v1" and
    .image == $image and .node == $node and
    (.glibc_version | test("^[0-9]+\\.[0-9]+$"))
  ' "$MSA_EVIDENCE/target-runtime.json" >/dev/null
MSA_TARGET_GLIBC=$(jq -er '.glibc_version' "$MSA_EVIDENCE/target-runtime.json")
```

Ready means the exact image started without `uvloop` and completed both strict
loopback POSTs: the retained 76-residue query and its one-residue mutant, each
with exactly 128 PDB70 A3M records. The target glibc is measured from that
image, never guessed.

## 4. Capture the UID-bound direct artifact

```console
python3 "$MSA_LANE/render_capture.py" \
  --donor-pod-json "$MSA_EVIDENCE/donor-pod.json" \
  > "$MSA_EVIDENCE/podsnapshotcontent.yaml"
kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start create \
  -f "$MSA_EVIDENCE/podsnapshotcontent.yaml"
kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start wait \
  --for=condition=Ready \
  podsnapshotcontent/msa-search-native-f7-v1-direct-hf93 --timeout=1800s
kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start get \
  podsnapshotcontent msa-search-native-f7-v1-direct-hf93 -o json \
  > "$MSA_EVIDENCE/podsnapshotcontent.json"
kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start logs \
  msa-search-native-f7-snapshot-agent-hf93 \
  > "$MSA_EVIDENCE/capture-agent.log"
```

Preserve the evidence above before freeing the GPU, then delete only the three
exact capture-stage objects:

```console
kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start delete job \
  msa-search-native-f7-donor-r1 --wait=true --timeout=300s
kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start delete pod \
  msa-search-native-f7-snapshot-agent-hf93 --wait=true --timeout=300s
kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start delete configmap \
  msa-search-native-f7-validator-r1 --wait=true --timeout=120s
```

## 5. Verify and prewarm the direct artifact

The CPU-only holder hashes every regular file with four readers, verifies the
direct manifest identity, and becomes Ready only after reading the complete
artifact.

```console
kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start create \
  -f "$MSA_LANE/artifact-holder.yaml"
kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start wait \
  --for=condition=Ready pod/msa-search-native-f7-holder-root-hf93 --timeout=1800s
kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start logs \
  msa-search-native-f7-holder-root-hf93 | tail -n 1 \
  > "$MSA_EVIDENCE/artifact-direct-receipt.json"
MSA_DIRECT_MANIFEST=$(jq -er '
  select(.schema == "archvteams.nebius.ai/msa-search-native-artifact-receipt/v1") |
  select(.checkpoint_id == "msa-search-native-f7-v1") |
  select(.image_io_mode == "direct") |
  select(.prewarm_bytes == .regular_file_bytes) |
  .manifest_sha256
' "$MSA_EVIDENCE/artifact-direct-receipt.json")
test "${#MSA_DIRECT_MANIFEST}" -eq 64
```

## 6. Run direct-mode `n=3`

The runner timestamps demand before creating the target. It binds the live Pod
UID, container ID, cgroup, Pod IP, image ID, and canonical PodSpec hash. It then
submits the CPU semantic probe before the one-shot restore worker. Each of the
three trials must finish two distinct ClusterIP calls and then pass the
digest-pinned in-target check that MMseqs fd 1 and API worker fd 24 still share
one pipe. `--cleanup` removes only that run's target, worker, probe, Service,
and binding objects.

```console
"$MSA_LANE/dynamo/run_n3.sh" \
  --run-prefix msa-direct \
  --evidence-root "$MSA_EVIDENCE" \
  --node "$MSA_NODE" \
  --kubeconfig "$MSA_KUBECONFIG" \
  --artifact-holder msa-search-native-f7-holder-root-hf93 \
  --checkpoint-id msa-search-native-f7-v1 \
  --target-glibc-version "$MSA_TARGET_GLIBC" \
  --image-io-mode direct \
  --artifact-manifest-sha256 "$MSA_DIRECT_MANIFEST" \
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
python3 "$MSA_LANE/render_buffered_variant.py" \
  --artifact-receipt "$MSA_EVIDENCE/artifact-direct-receipt.json" \
  > "$MSA_EVIDENCE/buffered-build.yaml"
kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start create \
  -f "$MSA_EVIDENCE/buffered-build.yaml"
kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start wait \
  --for=condition=Complete job/msa-search-native-f7-v2-buffered-build \
  --timeout=900s
kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start logs \
  job/msa-search-native-f7-v2-buffered-build | tail -n 1 \
  > "$MSA_EVIDENCE/buffered-build-receipt.json"
jq -e '
  .schema == "archvteams.nebius.ai/msa-search-buffered-build/v1" and
  .status == "PASS" and
  .checkpoint_id == "msa-search-native-f7-v2-buffered" and
  .image_io_mode == "buffered" and
  (.manifest_sha256 | test("^[0-9a-f]{64}$"))
' "$MSA_EVIDENCE/buffered-build-receipt.json" >/dev/null

kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start create \
  -f "$MSA_LANE/artifact-holder-buffered.yaml"
kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start wait \
  --for=condition=Ready pod/msa-search-native-f7-buffered-holder-root-hf93 \
  --timeout=1800s
kubectl --kubeconfig "$MSA_KUBECONFIG" -n nim-fast-start logs \
  msa-search-native-f7-buffered-holder-root-hf93 | tail -n 1 \
  > "$MSA_EVIDENCE/artifact-buffered-receipt.json"
MSA_BUFFERED_MANIFEST=$(jq -er '
  select(.schema == "archvteams.nebius.ai/msa-search-native-artifact-receipt/v1") |
  select(.checkpoint_id == "msa-search-native-f7-v2-buffered") |
  select(.image_io_mode == "buffered") |
  select(.prewarm_bytes == .regular_file_bytes) |
  .manifest_sha256
' "$MSA_EVIDENCE/artifact-buffered-receipt.json")
test "$MSA_BUFFERED_MANIFEST" = \
  "$(jq -er '.manifest_sha256' "$MSA_EVIDENCE/buffered-build-receipt.json")"

"$MSA_LANE/dynamo/run_provisioned_trial.sh" \
  --run-id msa-buffered-smoke \
  --evidence-root "$MSA_EVIDENCE" \
  --node "$MSA_NODE" \
  --kubeconfig "$MSA_KUBECONFIG" \
  --artifact-holder msa-search-native-f7-buffered-holder-root-hf93 \
  --checkpoint-id msa-search-native-f7-v2-buffered \
  --target-glibc-version "$MSA_TARGET_GLIBC" \
  --image-io-mode buffered \
  --artifact-manifest-sha256 "$MSA_BUFFERED_MANIFEST" \
  --cleanup
```

The buffered variant remains a candidate until this complete two-call smoke
reports `PASS`.

## 8. Run buffered-mode `n=3` and compare

```console
"$MSA_LANE/dynamo/run_n3.sh" \
  --run-prefix msa-buffered \
  --evidence-root "$MSA_EVIDENCE" \
  --node "$MSA_NODE" \
  --kubeconfig "$MSA_KUBECONFIG" \
  --artifact-holder msa-search-native-f7-buffered-holder-root-hf93 \
  --checkpoint-id msa-search-native-f7-v2-buffered \
  --target-glibc-version "$MSA_TARGET_GLIBC" \
  --image-io-mode buffered \
  --artifact-manifest-sha256 "$MSA_BUFFERED_MANIFEST" \
  --cleanup

jq -s '
  map({
    checkpoint_id,
    image_io_mode,
    trial_count,
    request_count,
    demand_to_two_semantic_seconds,
    worker_restore_seconds,
    statistics_seconds
  })
' "$MSA_EVIDENCE/n3-msa-direct-direct.json" \
  "$MSA_EVIDENCE/n3-msa-buffered-buffered.json" \
  > "$MSA_EVIDENCE/direct-vs-buffered.json"
```

Report all six demand values, both medians, all six worker restore values, and
the direct/buffered winner. Do not report the retained 3.186845-second
experimental baseline as the production-shaped result.

## 9. Cleanup after evidence is durable

The runner already deletes its exact run-scoped objects when `--cleanup` is
used. After copying all receipts, remove only the two named holder Pods and the
completed buffered-builder Job. Retain the PodSnapshotContent and both PVCs
until the qualification result and rollback decision are accepted.

Deleting either PVC is destructive and intentionally has no command in this
plan.
