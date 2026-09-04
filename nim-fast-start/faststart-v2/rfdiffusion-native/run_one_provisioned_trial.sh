#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

readonly allowed_server="${EXPECTED_API_SERVER:-https://kubernetes-api.example.invalid:443}"
readonly namespace="nim-fast-start"
readonly expected_contract_sha256="139bce29698bfd77a29847c5ddf22cad8a66eed3bab9cca9b180fd8387633140"
readonly expected_validator_sha256="691ee7d60aaf22f32cb05bb7c67fa0552b656f0d549015f3297516e1dd7b2317"
readonly expected_fixture_sha256="d4a6812d8951cf6594e6a0763f089e35f5a80b62acb3c117b2c5565228a7b161"

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly script_dir
readonly profile_path="$script_dir/profile.json"
readonly gate_path="$script_dir/worker-gate.json"
readonly contract_path="$script_dir/restore-interface.performance.json"
readonly validator_path="$script_dir/validate_rfdiffusion.py"
readonly fixture_path="$script_dir/fixtures/1UBQ.pdb"

run_id=""
image_io_mode=""
artifact_manifest_sha256=""
evidence_root=""
kubeconfig=""
artifact_holder=""
image_cache_holder=""
cleanup=0
allow_performance_worker=0

usage() {
  cat >&2 <<'USAGE'
usage: run_one_provisioned_trial.sh \
  --run-id RUN_ID \
  --image-io-mode direct|buffered \
  --artifact-manifest-sha256 SHA256 \
  --evidence-root ABSOLUTE_DIRECTORY \
  --kubeconfig ABSOLUTE_FILE \
  --artifact-holder READY_POD \
  --image-cache-holder READY_POD \
  --allow-performance-validation-worker [--cleanup]
USAGE
}

die_usage() {
  printf '%s\n' "$1" >&2
  usage
  exit 64
}

set_once() {
  local option_name=$1 current=$2 value=$3
  [[ -z $current ]] || die_usage "$option_name may be supplied only once"
  [[ -n $value ]] || die_usage "$option_name requires a value"
}

while (($# > 0)); do
  case "$1" in
    --run-id)
      (($# >= 2)) || die_usage "--run-id requires a value"
      set_once --run-id "$run_id" "$2"; run_id=$2; shift 2 ;;
    --image-io-mode)
      (($# >= 2)) || die_usage "--image-io-mode requires a value"
      set_once --image-io-mode "$image_io_mode" "$2"; image_io_mode=$2; shift 2 ;;
    --artifact-manifest-sha256)
      (($# >= 2)) || die_usage "--artifact-manifest-sha256 requires a value"
      set_once --artifact-manifest-sha256 "$artifact_manifest_sha256" "$2"
      artifact_manifest_sha256=$2; shift 2 ;;
    --evidence-root)
      (($# >= 2)) || die_usage "--evidence-root requires a value"
      set_once --evidence-root "$evidence_root" "$2"; evidence_root=$2; shift 2 ;;
    --kubeconfig)
      (($# >= 2)) || die_usage "--kubeconfig requires a value"
      set_once --kubeconfig "$kubeconfig" "$2"; kubeconfig=$2; shift 2 ;;
    --artifact-holder)
      (($# >= 2)) || die_usage "--artifact-holder requires a value"
      set_once --artifact-holder "$artifact_holder" "$2"; artifact_holder=$2; shift 2 ;;
    --image-cache-holder)
      (($# >= 2)) || die_usage "--image-cache-holder requires a value"
      set_once --image-cache-holder "$image_cache_holder" "$2"
      image_cache_holder=$2; shift 2 ;;
    --allow-performance-validation-worker)
      ((allow_performance_worker == 0)) || die_usage "worker override may be supplied only once"
      allow_performance_worker=1; shift ;;
    --cleanup)
      ((cleanup == 0)) || die_usage "--cleanup may be supplied only once"
      cleanup=1; shift ;;
    --help|-h)
      usage; exit 0 ;;
    *)
      die_usage "unknown argument: $1" ;;
  esac
done

[[ -n $run_id ]] || die_usage "--run-id is required"
[[ -n $image_io_mode ]] || die_usage "--image-io-mode is required"
[[ -n $artifact_manifest_sha256 ]] || die_usage "--artifact-manifest-sha256 is required"
[[ -n $evidence_root ]] || die_usage "--evidence-root is required"
[[ -n $kubeconfig ]] || die_usage "--kubeconfig is required"
[[ -n $artifact_holder ]] || die_usage "--artifact-holder is required"
[[ -n $image_cache_holder ]] || die_usage "--image-cache-holder is required"
[[ $run_id =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#run_id} -le 30 ]] || die_usage "invalid run ID"
[[ $image_io_mode == direct || $image_io_mode == buffered ]] || die_usage "invalid image I/O mode"
[[ $artifact_manifest_sha256 =~ ^[0-9a-f]{64}$ ]] || die_usage "invalid artifact manifest SHA-256"
[[ $artifact_holder =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#artifact_holder} -le 63 ]] || die_usage "invalid artifact holder"
[[ $image_cache_holder =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#image_cache_holder} -le 63 ]] || die_usage "invalid image cache holder"
[[ $evidence_root == /* && -d $evidence_root && ! -L $evidence_root ]] || die_usage "evidence root must be an existing absolute non-symlink directory"
[[ -d $evidence_root/runs && ! -L $evidence_root/runs ]] || die_usage "evidence root must contain a non-symlink runs directory"
[[ $kubeconfig == /* && -f $kubeconfig && ! -L $kubeconfig ]] || die_usage "kubeconfig must be an absolute regular non-symlink file"

for required in kubectl jq python3 sha256sum date install tail sleep; do
  command -v "$required" >/dev/null || { printf 'missing command: %s\n' "$required" >&2; exit 69; }
done

actual_contract_sha256=$(sha256sum "$contract_path")
actual_contract_sha256=${actual_contract_sha256%% *}
[[ $actual_contract_sha256 == "$expected_contract_sha256" ]] || {
  printf 'restore contract digest mismatch\n' >&2
  exit 78
}
actual_validator_sha256=$(sha256sum "$validator_path")
actual_validator_sha256=${actual_validator_sha256%% *}
[[ $actual_validator_sha256 == "$expected_validator_sha256" ]] || {
  printf 'semantic validator digest mismatch\n' >&2
  exit 78
}
actual_fixture_sha256=$(sha256sum "$fixture_path")
actual_fixture_sha256=${actual_fixture_sha256%% *}
[[ $actual_fixture_sha256 == "$expected_fixture_sha256" ]] || {
  printf 'semantic fixture digest mismatch\n' >&2
  exit 78
}
jq -e --arg image "$(jq -r .worker_image "$gate_path")" \
  --arg tool "$(jq -r .tool_bundle_manifest_sha256 "$gate_path")" \
  '.approved == true and .worker_image == $image and
   .tool_bundle.content_sha256 == $tool and
   (.worker_image | test("@sha256:[0-9a-f]{64}$")) and
   (.probe_image | test("@sha256:[0-9a-f]{64}$"))' "$contract_path" >/dev/null || {
  printf 'restore contract is not the exact performance contract\n' >&2
  exit 78
}
if ! jq -e '.release_ready == true' "$gate_path" >/dev/null; then
  ((allow_performance_worker == 1)) || {
    printf 'worker is performance-valid but not release-ready; explicit performance override is required\n' >&2
    exit 78
  }
  jq -e '.performance_validation_ready == true' "$gate_path" >/dev/null || exit 78
fi

target_node=$(jq -er '.hardware.retained_capture_node' "$profile_path")
checkpoint_id=$(jq -er --arg mode "$image_io_mode" '.artifacts[$mode].checkpoint_id' "$profile_path")
artifact_version=$(jq -er --arg mode "$image_io_mode" '.artifacts[$mode].artifact_version' "$profile_path")
profile_artifact_manifest_sha256=$(jq -er --arg mode "$image_io_mode" '.artifacts[$mode].manifest_sha256' "$profile_path")
artifact_pvc=$(jq -er '.storage.artifact_pvc' "$profile_path")
cache_pvc=$(jq -er '.storage.cache_pvc' "$profile_path")
cache_tree_sha256=$(jq -er '.retained_evidence.cache_tree_sha256' "$profile_path")
nim_image=$(jq -er '.model.image' "$profile_path")
readonly target_node checkpoint_id artifact_version profile_artifact_manifest_sha256 artifact_pvc cache_pvc cache_tree_sha256 nim_image
[[ $artifact_manifest_sha256 == "$profile_artifact_manifest_sha256" ]] || {
  printf 'artifact manifest does not match the selected pinned profile\n' >&2
  exit 78
}

server=$(kubectl --kubeconfig "$kubeconfig" config view --minify -o jsonpath='{.clusters[0].cluster.server}')
[[ $server == "$allowed_server" ]] || { printf 'kubeconfig is not bound to the allowed cluster\n' >&2; exit 78; }
trial_kubectl=(kubectl --kubeconfig "$kubeconfig" -n "$namespace")

node_json=$(kubectl --kubeconfig "$kubeconfig" get node "$target_node" -o json)
jq -e '
  .metadata.labels["nebius.com/gpu-name"] == "H100" and
  .status.capacity["nvidia.com/gpu"] == "1" and
  .status.allocatable["nvidia.com/gpu"] == "1" and
  ([.status.capacity | keys[] | select(startswith("nvidia.com/mig-"))] | length) == 0 and
  any(.status.conditions[]?; .type == "Ready" and .status == "True")
' <<<"$node_json" >/dev/null || {
  printf 'node does not match the pinned one-full-GPU H100 topology\n' >&2
  exit 69
}

pods_before=$(kubectl --kubeconfig "$kubeconfig" get pods -A \
  --field-selector "spec.nodeName=$target_node" -o json)
jq -e '
  [
    .items[] |
    select(.status.phase != "Succeeded" and .status.phase != "Failed") |
    (.spec.initContainers[]?, .spec.containers[]?) |
    ((.resources.requests["nvidia.com/gpu"] // "0") | tonumber)
  ] | add // 0 | . == 0
' <<<"$pods_before" >/dev/null || {
  printf 'the pinned H100 already has an active GPU request\n' >&2
  exit 69
}

holder_json=$("${trial_kubectl[@]}" get pod "$artifact_holder" -o json)
jq -e \
  --arg node "$target_node" \
  --arg checkpoint "$checkpoint_id" \
  --arg mode "$image_io_mode" \
  --arg manifest "$artifact_manifest_sha256" \
  --arg artifact_pvc "$artifact_pvc" \
  --arg cache_pvc "$cache_pvc" \
  --arg cache_tree "$cache_tree_sha256" '
    .spec.nodeName == $node and
    .metadata.labels["archvteams.nebius.ai/checkpoint-id"] == $checkpoint and
    .metadata.labels["archvteams.nebius.ai/image-io-mode"] == $mode and
    .metadata.annotations["archvteams.nebius.ai/artifact-manifest-sha256"] == $manifest and
    .metadata.annotations["archvteams.nebius.ai/cache-tree-sha256"] == $cache_tree and
    any(.status.conditions[]?; .type == "Ready" and .status == "True") and
    any(.spec.volumes[]?; .persistentVolumeClaim.claimName == $artifact_pvc) and
    any(.spec.volumes[]?; .persistentVolumeClaim.claimName == $cache_pvc)
  ' <<<"$holder_json" >/dev/null || {
  printf 'artifact holder is not Ready with the exact mode, artifact, and cache\n' >&2
  exit 69
}
holder_receipt=$("${trial_kubectl[@]}" logs "$artifact_holder" | tail -1)
jq -e -c \
  --arg mode "$image_io_mode" \
  --arg manifest "$artifact_manifest_sha256" \
  --arg cache_tree "$cache_tree_sha256" '
    .schema == "archvteams.nebius.ai/rfdiffusion-artifact-holder/v1" and
    .status == "PASS" and .image_io_mode == $mode and
    .manifest_sha256 == $manifest and
    .cache.status == "PASS" and .cache.tree_sha256 == $cache_tree and
    .cache.payload_read == true and
    (.cache.elapsed_seconds | type == "number" and . > 0) and
    (.elapsed_seconds | type == "number" and . >= 0) and
    (if $mode == "buffered" then .payload_read == true else .payload_read == false end)
  ' <<<"$holder_receipt" >/dev/null || {
  printf 'artifact holder receipt does not prove the exact pre-T0 storage state\n' >&2
  exit 69
}

image_holder_json=$("${trial_kubectl[@]}" get pod "$image_cache_holder" -o json)
nim_digest=${nim_image##*@}
jq -e \
  --arg node "$target_node" \
  --arg image "$nim_image" \
  --arg digest "$nim_digest" '
    .spec.nodeName == $node and
    .status.phase == "Running" and
    any(.status.conditions[]?; .type == "Ready" and .status == "True") and
    (.spec.containers | length) == 1 and
    .spec.containers[0].image == $image and
    ((.spec.containers[0].resources.requests["nvidia.com/gpu"] // "0") | tonumber) == 0 and
    (.status.containerStatuses | length) == 1 and
    .status.containerStatuses[0].ready == true and
    (.status.containerStatuses[0].state.running.startedAt | type == "string") and
    (.status.containerStatuses[0].containerID | startswith("containerd://")) and
    (.status.containerStatuses[0].imageID | sub("^docker-pullable://"; "")) == $image
  ' <<<"$image_holder_json" >/dev/null || {
  printf 'image cache holder is not Ready with the exact live image digest on the target node\n' >&2
  exit 69
}
image_holder_receipt=$(jq -c \
  --arg image "$nim_image" \
  --arg digest "$nim_digest" '
    {
      schema:"archvteams.nebius.ai/rfdiffusion-image-cache-holder/v1",
      status:"PASS",
      pod_name:.metadata.name,
      pod_uid:.metadata.uid,
      node:.spec.nodeName,
      image:$image,
      image_digest:$digest,
      live_image_id:.status.containerStatuses[0].imageID,
      container_id:.status.containerStatuses[0].containerID,
      running_started_at:.status.containerStatuses[0].state.running.startedAt,
      ready_transition_at:([.status.conditions[]|select(.type=="Ready" and .status=="True")][0].lastTransitionTime),
      gpu_request:0
    }
  ' <<<"$image_holder_json")

runtime_topology_json=$(jq -c '.runtime_topology' "$profile_path")
jq -e '
    keys == ["source_capture_id","validator_mount_path","validator_size_limit","validator_volume_type"] and
    .validator_volume_type == "bounded_empty_dir" and
    .validator_mount_path == "/validator" and
    (.validator_size_limit | test("^[1-9][0-9]*(Ki|Mi|Gi)$")) and
    (.source_capture_id | test("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"))
  ' <<<"$runtime_topology_json" >/dev/null || {
  printf 'runtime topology is not the exact d5ce-compatible bounded emptyDir contract\n' >&2
  exit 69
}

readonly run_dir="$evidence_root/runs/$run_id"
readonly target="rfd-target-$run_id"
readonly worker="rfd-restore-$run_id"
readonly probe="rfd-semantic-$run_id"
readonly canary="rfd-canary-$run_id"
[[ ! -e $run_dir && ! -L $run_dir ]] || { printf 'run directory already exists\n' >&2; exit 73; }
mkdir -m 0700 -- "$run_dir"
printf '%s\n' "$holder_receipt" > "$run_dir/holder-receipt.json"
printf '%s\n' "$image_holder_receipt" > "$run_dir/image-holder-receipt.json"
printf '%s\n' "$image_holder_json" > "$run_dir/image-holder-pod.json"
printf '%s\n' "$runtime_topology_json" > "$run_dir/runtime-topology.json"
jq -n \
  --arg mode "$image_io_mode" \
  --slurpfile receipt "$run_dir/holder-receipt.json" \
  --slurpfile image "$run_dir/image-holder-receipt.json" '
    ($receipt[0]) as $r |
    ($image[0]) as $i |
    {
      schema:"archvteams.nebius.ai/rfdiffusion-storage-state/v1",
      state:(if $mode == "buffered" then "buffered_fully_prewarmed" else "direct_o_direct_no_artifact_payload_prewarm" end),
      image_io_mode:$mode,
      storage_attached_before_t0:true,
      image_present_before_t0:($i.status == "PASS"),
      image_holder:$i,
      prewarm_outside_t0:true,
      artifact_payload_read:$r.payload_read,
      artifact_regular_bytes:$r.regular_bytes,
      artifact_prewarm_seconds:$r.elapsed_seconds,
      cache_payload_read:$r.cache.payload_read,
      cache_regular_bytes:$r.cache.regular_bytes,
      cache_prewarm_seconds:$r.cache.elapsed_seconds,
      total_pre_t0_full_read_seconds:($r.elapsed_seconds + $r.cache.elapsed_seconds)
    }
  ' > "$run_dir/storage-state.json"
install -m 0600 -- "$contract_path" "$run_dir/restore-interface.json"
install -m 0600 -- "$profile_path" "$run_dir/profile.json"
install -m 0600 -- "$gate_path" "$run_dir/worker-gate.json"
install -m 0600 -- "$fixture_path" "$run_dir/1UBQ.pdb"
(cd -- "$run_dir" && sha256sum restore-interface.json profile.json worker-gate.json 1UBQ.pdb > inputs.sha256)

demand_at=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
jq -n \
  --arg schema "archvteams.nebius.ai/rfdiffusion-faststart-run/v1" \
  --arg run_id "$run_id" \
  --arg demand_at "$demand_at" \
  --arg target_node "$target_node" \
  --arg checkpoint_id "$checkpoint_id" \
  --arg artifact_version "$artifact_version" \
  --arg artifact_manifest_sha256 "$artifact_manifest_sha256" \
  --arg artifact_pvc "$artifact_pvc" \
  --arg cache_pvc "$cache_pvc" \
  --arg image_io_mode "$image_io_mode" \
  '{schema:$schema,run_id:$run_id,demand_at:$demand_at,target_node:$target_node,
    checkpoint_id:$checkpoint_id,artifact_version:$artifact_version,
    artifact_manifest_sha256:$artifact_manifest_sha256,artifact_pvc:$artifact_pvc,
    cache_pvc:$cache_pvc,image_io_mode:$image_io_mode}' > "$run_dir/run.json"

python3 "$script_dir/render.py" target \
  --contract "$run_dir/restore-interface.json" \
  --run-config "$run_dir/run.json" > "$run_dir/target.yaml"
date -u +%Y-%m-%dT%H:%M:%S.%NZ > "$run_dir/target-submit-at.txt"
"${trial_kubectl[@]}" create -f "$run_dir/target.yaml"
"${trial_kubectl[@]}" wait --for=jsonpath='{.status.containerStatuses[0].state.running}' \
  "pod/$target" --timeout=600s
"${trial_kubectl[@]}" get pod "$target" -o json > "$run_dir/target-before-binding.json"

python3 "$script_dir/bind_target.py" \
  --contract "$run_dir/restore-interface.json" \
  --run-config "$run_dir/run.json" \
  --pod-json "$run_dir/target-before-binding.json" \
  --collected-at "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" \
  --binding-output "$run_dir/binding.json" \
  --patch-output "$run_dir/target-pod-spec.patch.json"
"${trial_kubectl[@]}" patch pod "$target" --type=json \
  --patch-file="$run_dir/target-pod-spec.patch.json" -o json > "$run_dir/target-patch-response.json"
"${trial_kubectl[@]}" get pod "$target" -o json > "$run_dir/target-bound.json"

# The tokenless, CPU-only probe starts first and waits through the run-scoped
# ClusterIP while the one-shot worker restores the process tree.
python3 "$script_dir/render.py" probe \
  --contract "$run_dir/restore-interface.json" \
  --run-config "$run_dir/run.json" \
  --binding "$run_dir/binding.json" > "$run_dir/semantic-probe.yaml"
date -u +%Y-%m-%dT%H:%M:%S.%NZ > "$run_dir/probe-submit-at.txt"
"${trial_kubectl[@]}" create -f "$run_dir/semantic-probe.yaml"

python3 "$script_dir/render.py" restore \
  --contract "$run_dir/restore-interface.json" \
  --run-config "$run_dir/run.json" \
  --binding "$run_dir/binding.json" > "$run_dir/restore-worker.yaml"
date -u +%Y-%m-%dT%H:%M:%S.%NZ > "$run_dir/worker-submit-at.txt"
"${trial_kubectl[@]}" create -f "$run_dir/restore-worker.yaml"

wait_for_job() {
  local name=$1 output=$2 attempt
  for ((attempt=0; attempt<1800; attempt++)); do
    "${trial_kubectl[@]}" get job "$name" -o json > "$output"
    jq -e '(.status.succeeded // 0) == 1' "$output" >/dev/null && return 0
    jq -e '(.status.failed // 0) > 0' "$output" >/dev/null && return 1
    sleep 1
  done
  return 124
}

if ! wait_for_job "$worker" "$run_dir/worker-job.json"; then
  "${trial_kubectl[@]}" get pods -l "job-name=$worker" -o json > "$run_dir/worker-pods.failed.json"
  exit 1
fi
"${trial_kubectl[@]}" get pods -l "job-name=$worker" -o json > "$run_dir/worker-pods.json"
worker_pod=$(jq -er 'select((.items|length)==1) | .items[0].metadata.name' "$run_dir/worker-pods.json")
"${trial_kubectl[@]}" get pod "$worker_pod" -o json > "$run_dir/worker-pod.json"
"${trial_kubectl[@]}" logs "$worker_pod" > "$run_dir/worker.log"
tail -1 "$run_dir/worker.log" | jq -e -c \
  --arg checkpoint "$checkpoint_id" \
  --arg manifest "$artifact_manifest_sha256" \
  'select(.status=="succeeded" and .checkpoint_id==$checkpoint and .checkpoint_manifest_sha256==$manifest)' \
  > "$run_dir/worker-receipt.json"

"${trial_kubectl[@]}" wait --for=condition=Ready "pod/$target" --timeout=600s
"${trial_kubectl[@]}" get pod "$target" -o json > "$run_dir/target-ready.json"

if ! wait_for_job "$probe" "$run_dir/probe-job.json"; then
  "${trial_kubectl[@]}" get pods -l "job-name=$probe" -o json > "$run_dir/probe-pods.failed.json"
  exit 1
fi
"${trial_kubectl[@]}" get pods -l "job-name=$probe" -o json > "$run_dir/probe-pods.json"
probe_pod=$(jq -er 'select((.items|length)==1) | .items[0].metadata.name' "$run_dir/probe-pods.json")
"${trial_kubectl[@]}" get pod "$probe_pod" -o json > "$run_dir/probe-pod.json"
"${trial_kubectl[@]}" logs "$probe_pod" > "$run_dir/semantic-probe.log"
tail -1 "$run_dir/semantic-probe.log" | jq -e -c \
  'select(.schema=="archvteams.nebius.ai/rfdiffusion-semantic-probe/v1" and
          .status=="PASS" and .ok==true and .request_count==2 and
          .passed_case_count==2 and .failed_case_count==0 and (.cases|length)==2)' \
  > "$run_dir/semantic-summary.json"

"${trial_kubectl[@]}" get pod "$target" -o json > "$run_dir/target-final.json"
"${trial_kubectl[@]}" get service "$canary" -o json > "$run_dir/canary-service.json"
"${trial_kubectl[@]}" get endpointslices.discovery.k8s.io \
  -l "kubernetes.io/service-name=$canary" -o json > "$run_dir/canary-endpointslices.json"
jq -e \
  --arg uid "$(jq -r .pod_uid "$run_dir/binding.json")" \
  --arg ip "$(jq -r .pod_ip "$run_dir/binding.json")" '
    [.items[].endpoints[]? | select(.targetRef.uid==$uid and (.addresses|index($ip)) != null)] | length == 1
  ' "$run_dir/canary-endpointslices.json" >/dev/null || {
  printf 'ClusterIP endpoint is not bound to the exact restored Pod UID and IP\n' >&2
  exit 1
}

python3 - "$run_dir" "$nim_image" "$script_dir/.." <<'PY' > "$run_dir/trial-summary.json"
import json
import sys
from datetime import datetime
from pathlib import Path

directory = Path(sys.argv[1])
nim_image = sys.argv[2]
sys.path.insert(0, sys.argv[3])
from timing_evidence import build_timing_evidence

run = json.loads((directory / "run.json").read_text())
binding = json.loads((directory / "binding.json").read_text())
worker = json.loads((directory / "worker-receipt.json").read_text())
semantic = json.loads((directory / "semantic-summary.json").read_text())
target = json.loads((directory / "target-final.json").read_text())
storage = json.loads((directory / "storage-state.json").read_text())
target_submit_at = (directory / "target-submit-at.txt").read_text(encoding="ascii").strip()
call_2_response_received_at = semantic["cases"][1]["response_received_at"]
timings = build_timing_evidence(
    run, semantic, target, target_submit_at=target_submit_at
)
if timings["timing_evidence"]["demand_at"] != target_submit_at:
    raise RuntimeError("timing evidence is not rooted at the exact target-submit timestamp")
parse_timestamp = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
validation_finished_at = semantic["validation_finished_at"]
demand_to_validation_seconds = round(
    (parse_timestamp(validation_finished_at) - parse_timestamp(target_submit_at)).total_seconds(),
    6,
)
validation_overhang_seconds = round(
    (
        parse_timestamp(validation_finished_at)
        - parse_timestamp(call_2_response_received_at)
    ).total_seconds(),
    6,
)
if demand_to_validation_seconds <= 0 or validation_overhang_seconds < 0:
    raise RuntimeError("semantic validation timestamps are not ordered after T0/call 2")
result = {
    "schema": "archvteams.nebius.ai/rfdiffusion-native-trial-summary/v1",
    "run_id": run["run_id"],
    "status": "PASS",
    "model": "RFdiffusion",
    "image": nim_image,
    "gpu_topology": "1x NVIDIA H100, full GPU, non-MIG",
    "image_io_mode": run["image_io_mode"],
    "checkpoint_id": run["checkpoint_id"],
    "artifact_manifest_sha256": run["artifact_manifest_sha256"],
    "pod_uid": binding["pod_uid"],
    "pod_spec_sha256": binding["pod_spec_sha256"],
    "semantic_request_count": 2,
    "semantic_response_sha256": [case["response_sha256"] for case in semantic["cases"]],
    "t0_basis": "target-submit-at immediately before kubectl create",
    "target_submit_at": target_submit_at,
    "prepared_at": run["demand_at"],
    "call_2_response_received_at": call_2_response_received_at,
    "semantic_validation_finished_at": validation_finished_at,
    "demand_to_semantic_validation_seconds": demand_to_validation_seconds,
    "semantic_validation_overhang_seconds": validation_overhang_seconds,
    "storage_state": storage,
    "worker_receipt": worker,
    "semantic": semantic,
    **timings,
}
print(json.dumps(result, sort_keys=True, indent=2))
PY

jq '{run_id,status,image_io_mode,demand_to_http_ready_seconds,
     demand_to_kubernetes_ready_seconds,semantic_request_1_seconds,
     semantic_request_2_seconds,demand_to_two_semantic_seconds,
     restore_seconds:(.worker_receipt.duration_ms / 1000)}' "$run_dir/trial-summary.json"

if ((cleanup == 1)); then
  "${trial_kubectl[@]}" delete -f "$run_dir/semantic-probe.yaml" --ignore-not-found --wait=true --timeout=180s
  "${trial_kubectl[@]}" delete -f "$run_dir/restore-worker.yaml" --ignore-not-found --wait=true --timeout=180s
  "${trial_kubectl[@]}" delete -f "$run_dir/target.yaml" --ignore-not-found --wait=true --timeout=300s
fi
