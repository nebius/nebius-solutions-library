#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

readonly allowed_server="${EXPECTED_API_SERVER:-https://kubernetes-api.example.invalid:443}"
readonly namespace="nim-fast-start"
readonly expected_contract_sha256="f5a4e9c5fad402d69bc35acdc58f66f89c409c1b8c7417197dc107591f736915"
readonly expected_validator_sha256="113747668c537e83c61648e193419248696e144d5b0598b1f6ca7d38efabe496"

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly script_dir
readonly profile_path="$script_dir/profile.json"
readonly gate_path="$script_dir/worker-gate.json"
readonly contract_path="$script_dir/restore-interface.performance.json"
readonly validator_path="$script_dir/validate_evo2.py"

run_id=""
image_io_mode=""
artifact_manifest_sha256=""
evidence_root=""
kubeconfig=""
artifact_holder=""
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
[[ $run_id =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#run_id} -le 30 ]] || die_usage "invalid run ID"
[[ $image_io_mode == direct || $image_io_mode == buffered ]] || die_usage "invalid image I/O mode"
[[ $artifact_manifest_sha256 =~ ^[0-9a-f]{64}$ ]] || die_usage "invalid artifact manifest SHA-256"
[[ $artifact_holder =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#artifact_holder} -le 63 ]] || die_usage "invalid artifact holder"
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
artifact_pvc=$(jq -er '.storage.artifact_pvc' "$profile_path")
cache_pvc=$(jq -er '.storage.cache_pvc' "$profile_path")
nim_image=$(jq -er '.model.image' "$profile_path")
worker_image=$(jq -er '.worker_image' "$contract_path")
probe_image=$(jq -er '.probe_image' "$contract_path")
readonly target_node checkpoint_id artifact_version artifact_pvc cache_pvc nim_image
readonly worker_image probe_image

server=$(kubectl --kubeconfig "$kubeconfig" config view --minify -o jsonpath='{.clusters[0].cluster.server}')
[[ $server == "$allowed_server" ]] || { printf 'kubeconfig is not bound to the allowed cluster\n' >&2; exit 78; }
trial_kubectl=(kubectl --kubeconfig "$kubeconfig" -n "$namespace")

node_json=$(kubectl --kubeconfig "$kubeconfig" get node "$target_node" -o json)
jq -e '
  .metadata.labels["nebius.com/gpu-name"] == "H200" and
  .status.capacity["nvidia.com/gpu"] == "1" and
  .status.allocatable["nvidia.com/gpu"] == "1" and
  ([.status.capacity | keys[] | select(startswith("nvidia.com/mig-"))] | length) == 0 and
  any(.status.conditions[]?; .type == "Ready" and .status == "True")
' <<<"$node_json" >/dev/null || {
  printf 'node does not match the pinned one-full-GPU H200 topology\n' >&2
  exit 69
}
for cached_image in "$nim_image" "$worker_image" "$probe_image"; do
  jq -e --arg image "$cached_image" '
    any(.status.images[]?.names[]?; . == $image)
  ' <<<"$node_json" >/dev/null || {
    printf 'required image is not already present on the warm H200: %s\n' "$cached_image" >&2
    exit 69
  }
done

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
  printf 'the pinned H200 already has an active GPU request\n' >&2
  exit 69
}

holder_json=$("${trial_kubectl[@]}" get pod "$artifact_holder" -o json)
jq -e \
  --arg node "$target_node" \
  --arg checkpoint "$checkpoint_id" \
  --arg mode "$image_io_mode" \
  --arg manifest "$artifact_manifest_sha256" \
  --arg artifact_pvc "$artifact_pvc" \
  --arg cache_pvc "$cache_pvc" '
    .spec.nodeName == $node and
    .metadata.labels["archvteams.nebius.ai/checkpoint-id"] == $checkpoint and
    .metadata.labels["archvteams.nebius.ai/image-io-mode"] == $mode and
    .metadata.annotations["archvteams.nebius.ai/artifact-manifest-sha256"] == $manifest and
    any(.status.conditions[]?; .type == "Ready" and .status == "True") and
    any(.spec.volumes[]?; .persistentVolumeClaim.claimName == $artifact_pvc) and
    any(.spec.volumes[]?; .persistentVolumeClaim.claimName == $cache_pvc)
' <<<"$holder_json" >/dev/null || {
  printf 'artifact holder is not Ready with the exact mode, artifact, and cache\n' >&2
  exit 69
}
holder_receipt=$("${trial_kubectl[@]}" logs "$artifact_holder" -c holder | tail -n 1)
jq -e \
  --arg mode "$image_io_mode" \
  --arg manifest "$artifact_manifest_sha256" '
    .schema == "archvteams.nebius.ai/evo2-artifact-holder/v1" and
    .status == "PASS" and
    .image_io_mode == $mode and
    .manifest_sha256 == $manifest and
    .regular_file_count >= 2 and .regular_bytes > 0 and
    (.elapsed_seconds | type) == "number" and .elapsed_seconds >= 0 and
    (.started_at | type) == "string" and (.started_at | length) > 0 and
    (.finished_at | type) == "string" and (.finished_at | length) > 0 and
    .payload_read == ($mode == "buffered")
  ' <<<"$holder_receipt" >/dev/null || {
  printf 'artifact holder receipt does not prove the requested storage state\n' >&2
  exit 69
}

readonly run_dir="$evidence_root/runs/$run_id"
readonly target="e2-target-$run_id"
readonly worker="e2-restore-$run_id"
readonly probe="e2-semantic-$run_id"
readonly canary="e2-canary-$run_id"
[[ ! -e $run_dir && ! -L $run_dir ]] || { printf 'run directory already exists\n' >&2; exit 73; }
mkdir -m 0700 -- "$run_dir"
install -m 0600 -- "$contract_path" "$run_dir/restore-interface.json"
install -m 0600 -- "$profile_path" "$run_dir/profile.json"
install -m 0600 -- "$gate_path" "$run_dir/worker-gate.json"
printf '%s\n' "$holder_receipt" > "$run_dir/artifact-holder-receipt.json"
jq '{metadata:{name:.metadata.name,uid:.metadata.uid,creationTimestamp:.metadata.creationTimestamp},
     spec:{nodeName:.spec.nodeName,volumes:.spec.volumes},
     status:{phase:.status.phase,conditions:.status.conditions}}' \
  <<<"$holder_json" > "$run_dir/artifact-holder.json"
jq --arg nim_image "$nim_image" --arg worker_image "$worker_image" \
  --arg probe_image "$probe_image" '
    {metadata:{name:.metadata.name,uid:.metadata.uid,labels:.metadata.labels},
     status:{capacity:.status.capacity,allocatable:.status.allocatable,
             conditions:.status.conditions},
     cached_images:[$nim_image,$worker_image,$probe_image]}
  ' <<<"$node_json" > "$run_dir/warm-node-preflight.json"
(cd -- "$run_dir" && sha256sum restore-interface.json profile.json worker-gate.json > inputs.sha256)

demand_at=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
jq -n \
  --arg schema "archvteams.nebius.ai/evo2-faststart-run/v1" \
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
  'select(.schema=="archvteams.nebius.ai/evo2-semantic-probe/v1" and
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
holder = json.loads((directory / "artifact-holder-receipt.json").read_text())
holder_pod = json.loads((directory / "artifact-holder.json").read_text())
target_submit_at = (directory / "target-submit-at.txt").read_text().strip()
timings = build_timing_evidence(
    run, semantic, target, target_submit_at=target_submit_at
)

def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

holder_ready = [
    condition["lastTransitionTime"]
    for condition in holder_pod["status"]["conditions"]
    if condition.get("type") == "Ready" and condition.get("status") == "True"
]
if len(holder_ready) != 1:
    raise ValueError("artifact holder has no unique Ready timestamp")
if timestamp(holder["finished_at"]) > timestamp(target_submit_at):
    raise ValueError("artifact verification/prewarm did not finish before T0")
if timestamp(holder_ready[0]) > timestamp(target_submit_at):
    raise ValueError("artifact and cache PVCs were not Ready on the H200 before T0")
payload_read = holder["payload_read"]
storage_state = {
    "artifact_and_cache_pvcs_attached_before_t0": True,
    "artifact_holder_ready_at": holder_ready[0],
    "artifact_regular_file_count": holder["regular_file_count"],
    "artifact_regular_bytes": holder["regular_bytes"],
    "artifact_payload_read_before_t0": payload_read,
    "artifact_prewarm_seconds": holder["elapsed_seconds"] if payload_read else None,
    "artifact_prewarm_finished_at": holder["finished_at"] if payload_read else None,
    "artifact_prewarm_excluded_from_t0": payload_read,
    "page_cache_state": (
        "fully-prewarmed-buffered" if payload_read
        else "not-preloaded-direct-o_direct"
    ),
}
result = {
    "schema": "archvteams.nebius.ai/evo2-native-trial-summary/v1",
    "run_id": run["run_id"],
    "status": "PASS",
    "model": "Evo2-40B",
    "image": nim_image,
    "gpu_topology": "1x NVIDIA H200 SXM, full GPU, non-MIG",
    "image_io_mode": run["image_io_mode"],
    "checkpoint_id": run["checkpoint_id"],
    "artifact_manifest_sha256": run["artifact_manifest_sha256"],
    "pod_uid": binding["pod_uid"],
    "pod_spec_sha256": binding["pod_spec_sha256"],
    "semantic_request_count": 2,
    "semantic_response_sha256": [case["response_sha256"] for case in semantic["cases"]],
    "warm_instance_contract": {
        "node_ready_before_t0": True,
        "target_image_cached_before_t0": True,
        "restore_worker_image_cached_before_t0": True,
        "semantic_probe_image_cached_before_t0": True,
        "storage_attached_before_t0": True,
    },
    "storage_state": storage_state,
    "artifact_holder_receipt": holder,
    "worker_receipt": worker,
    "semantic": semantic,
    **timings,
}
print(json.dumps(result, sort_keys=True, indent=2))
PY

jq '{run_id,status,image_io_mode,demand_to_http_ready_seconds,
     demand_to_kubernetes_ready_seconds,semantic_request_1_seconds,
     semantic_request_2_seconds,demand_to_two_semantic_seconds,
     storage_state,
     restore_seconds:(.worker_receipt.duration_ms / 1000)}' "$run_dir/trial-summary.json"

if ((cleanup == 1)); then
  "${trial_kubectl[@]}" delete -f "$run_dir/semantic-probe.yaml" --ignore-not-found --wait=true --timeout=180s
  "${trial_kubectl[@]}" delete -f "$run_dir/restore-worker.yaml" --ignore-not-found --wait=true --timeout=180s
  "${trial_kubectl[@]}" delete -f "$run_dir/target.yaml" --ignore-not-found --wait=true --timeout=300s
fi
