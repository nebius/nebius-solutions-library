#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly allowed_server="${EXPECTED_API_SERVER:-https://kubernetes-api.example.invalid:443}"
readonly allowed_context="${EXPECTED_CONTEXT:-archvteams-2407-example}"
readonly allowed_node="${TARGET_NODE:-gpu-node-a.example.invalid}"
readonly target_image='nvcr.io/nim/colabfold/msa-search@sha256:944f3cf845761be8e42b33147ae08b68c61eca7cad67bf5251e1708d03c0165c'
readonly namespace='nim-fast-start'
readonly cache_pvc="${MSA_CACHE_PVC:-msa-search-cache-example}"
readonly cache_holder="${MSA_CACHE_HOLDER:-msa-search-cache-holder-example}"
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly script_dir

usage() {
  printf 'usage: %s --kubeconfig ABS_PATH --evidence-root ABS_DIR --batch-id DNS_LABEL\n' "$0" >&2
}

kubeconfig=''
evidence_root=''
batch_id=''
while (($#)); do
  case "$1" in
    --kubeconfig) kubeconfig=${2:-}; shift 2 ;;
    --evidence-root) evidence_root=${2:-}; shift 2 ;;
    --batch-id) batch_id=${2:-}; shift 2 ;;
    *) usage; exit 64 ;;
  esac
done
[[ $kubeconfig == /* && -f $kubeconfig && ! -L $kubeconfig ]] || { usage; exit 64; }
[[ $evidence_root == /* ]] || { usage; exit 64; }
[[ $batch_id =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#batch_id} -le 40 ]] || { usage; exit 64; }
for dns_label in "$namespace" "$cache_pvc" "$cache_holder"; do
  [[ $dns_label =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#dns_label} -le 63 ]] || {
    printf 'runtime Kubernetes identity is not a DNS label: %s\n' "$dns_label" >&2
    exit 64
  }
done
[[ $allowed_node =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ && ${#allowed_node} -le 253 ]] || {
  printf 'target node is not a DNS subdomain\n' >&2
  exit 64
}

install -d -m 0700 -- "$evidence_root"
readonly -a kubectl=(kubectl --kubeconfig "$kubeconfig" --context "$allowed_context" -n "$namespace")
actual_context=$(kubectl --kubeconfig "$kubeconfig" config current-context)
[[ $actual_context == "$allowed_context" ]] || { printf 'kube context mismatch\n' >&2; exit 78; }
actual_server=$(kubectl --kubeconfig "$kubeconfig" --context "$allowed_context" config view --minify -o jsonpath='{.clusters[0].cluster.server}')
[[ $actual_server == "$allowed_server" ]] || { printf 'cluster identity mismatch\n' >&2; exit 78; }

inputs_name="${batch_id}-inputs"
preload_name="${batch_id}-image-preload"
preload_created=0
inputs_created=0
created_jobs=()
finish() {
  local exit_code=$?
  local created_job
  trap - EXIT
  set +e
  for created_job in "${created_jobs[@]}"; do
    "${kubectl[@]}" delete job "$created_job" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1
  done
  if ((preload_created == 1)); then
    "${kubectl[@]}" delete pod "$preload_name" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1
  fi
  if ((inputs_created == 1)); then
    "${kubectl[@]}" delete configmap "$inputs_name" --ignore-not-found --wait=true --timeout=60s >/dev/null 2>&1
  fi
  if ((exit_code != 0)); then
    jq -n -c \
      --arg batch_id "$batch_id" \
      --argjson exit_code "$exit_code" \
      --arg excluded_at "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" \
      '{schema:"archvteams.nebius.ai/msa-search-conventional-exclusion/v1",status:"EXCLUDED",batch_id:$batch_id,exit_code:$exit_code,excluded_at:$excluded_at,reason:"cohort did not complete three qualifying trials; no aggregate was admitted"}' \
      > "$evidence_root/EXCLUDED.json"
  fi
  exit "$exit_code"
}
trap finish EXIT

for reserved_resource in \
  "pod/$preload_name" \
  "configmap/$inputs_name" \
  "job/${batch_id}-r1" \
  "job/${batch_id}-r2" \
  "job/${batch_id}-r3"; do
  if "${kubectl[@]}" get "$reserved_resource" >/dev/null 2>&1; then
    printf 'run-scoped resource already exists: %s\n' "$reserved_resource" >&2
    exit 78
  fi
done

"${kubectl[@]}" get node "$allowed_node" -o json > "$evidence_root/node.json"
jq -e --arg node "$allowed_node" '
  .metadata.name==$node and
  any(.status.conditions[]; .type=="Ready" and .status=="True") and
  .metadata.labels["nebius.com/gpu-name"]=="H100" and
  .status.allocatable["nvidia.com/gpu"]=="1"
' "$evidence_root/node.json" >/dev/null
"${kubectl[@]}" get pvc "$cache_pvc" -o json > "$evidence_root/cache-pvc.json"
jq -e '.status.phase=="Bound"' "$evidence_root/cache-pvc.json" >/dev/null

sed \
  -e "s|@@CACHE_HOLDER@@|${cache_holder}|g" \
  -e "s|@@TARGET_NODE@@|${allowed_node}|g" \
  -e "s|@@CACHE_PVC@@|${cache_pvc}|g" \
  "$script_dir/conventional-cache-holder.yaml" > "$evidence_root/cache-holder.yaml"
if ! "${kubectl[@]}" get pod "$cache_holder" >/dev/null 2>&1; then
  "${kubectl[@]}" create -f "$evidence_root/cache-holder.yaml"
fi
"${kubectl[@]}" wait --for=condition=Ready "pod/$cache_holder" --timeout=300s
"${kubectl[@]}" get pod "$cache_holder" -o json > "$evidence_root/cache-holder.json"
jq -e --arg node "$allowed_node" --arg claim "$cache_pvc" '
  .spec.nodeName==$node and
  .status.phase=="Running" and
  any(.status.conditions[]; .type=="Ready" and .status=="True") and
  (.spec.containers|length)==1 and
  (.spec.containers[0].resources.requests["nvidia.com/gpu"] // "0" | tonumber)==0 and
  any(.spec.volumes[]; .persistentVolumeClaim.claimName==$claim)
' "$evidence_root/cache-holder.json" >/dev/null
"${kubectl[@]}" logs "$cache_holder" > "$evidence_root/cache-holder.log"
tail -1 "$evidence_root/cache-holder.log" | jq -e -c '
  select(.schema=="archvteams.nebius.ai/msa-search-conventional-cache-holder/v1" and
         .status=="PASS" and .storage_attached==true and
         .prewarm_outside_t0==true and .prewarm_bytes>0 and
         .prewarm_elapsed_seconds>=0 and
         (.prewarm_started_at|type=="string") and
         (.prewarm_completed_at|type=="string"))
' > "$evidence_root/cache-holder-receipt.json"

sed \
  -e "s|@@PRELOAD_NAME@@|${preload_name}|g" \
  -e "s|@@TARGET_NODE@@|${allowed_node}|g" \
  -e "s|@@TARGET_IMAGE@@|${target_image}|g" \
  "$script_dir/conventional-image-preload.yaml.tmpl" > "$evidence_root/image-preload.yaml"
preload_created=1
"${kubectl[@]}" create -f "$evidence_root/image-preload.yaml"
"${kubectl[@]}" wait --for=condition=Ready "pod/$preload_name" --timeout=600s
"${kubectl[@]}" get pod "$preload_name" -o json > "$evidence_root/image-preload-pod.json"
preload_uid=$(jq -er '.metadata.uid' "$evidence_root/image-preload-pod.json")
"${kubectl[@]}" get events --field-selector "involvedObject.uid=$preload_uid" -o json \
  > "$evidence_root/image-preload-events.json"
jq -e --arg image "$target_image" --arg node "$allowed_node" '
  .spec.nodeName==$node and
  .status.phase=="Running" and
  (.spec.containers|length)==1 and
  .spec.containers[0].image==$image and
  (.spec.containers[0].resources.requests["nvidia.com/gpu"] // "0" | tonumber)==0 and
  (.status.containerStatuses|length)==1 and
  .status.containerStatuses[0].ready==true and
  .status.containerStatuses[0].restartCount==0 and
  (.status.containerStatuses[0].imageID|type=="string" and length>0)
' "$evidence_root/image-preload-pod.json" >/dev/null
"${kubectl[@]}" delete pod "$preload_name" --wait=true --timeout=120s
preload_created=0
if "${kubectl[@]}" get pod "$preload_name" > "$evidence_root/image-preload-after-delete.txt" 2>&1; then
  printf 'setup-only image preloader remains after deletion\n' >&2
  exit 1
fi
jq -n -c \
  --arg image "$target_image" \
  --arg image_id "$(jq -r '.status.containerStatuses[0].imageID' "$evidence_root/image-preload-pod.json")" \
  --arg node "$allowed_node" \
  --arg verified_at "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" \
  '{schema:"archvteams.nebius.ai/msa-search-image-residency/v1",status:"PASS",image:$image,image_id:$image_id,node:$node,gpu_request:0,preloaded_outside_t0:true,preloader_absent_after_setup:true,verified_at:$verified_at}' \
  > "$evidence_root/image-residency-receipt.json"

inputs_created=1
"${kubectl[@]}" create configmap "$inputs_name" \
  --from-file=validate_msa_search.py="$script_dir/validate_msa_search.py" \
  --from-file=request-pdb70.json="$script_dir/fixtures/request-pdb70.json" \
  --from-file=verify_mmseqs_pipe.py="$script_dir/verify_mmseqs_pipe.py"

for run in 1 2 3; do
  run_id="${batch_id}-r${run}"
  run_dir="$evidence_root/$run_id"
  install -d -m 0700 -- "$run_dir"

  kubectl --kubeconfig "$kubeconfig" --context "$allowed_context" get pods --all-namespaces \
    --field-selector "spec.nodeName=$allowed_node" -o json > "$run_dir/node-pods-preflight.json"
  jq -e '
    def gpu_requests:
      ([.spec.containers[]? | (.resources.requests["nvidia.com/gpu"] // "0" | tonumber)] | add // 0) as $app
      | ([.spec.initContainers[]? | (.resources.requests["nvidia.com/gpu"] // "0" | tonumber)] | if length==0 then 0 else max end) as $init
      | [$app, $init] | max;
    [ .items[]
      | select(.status.phase!="Succeeded" and .status.phase!="Failed")
      | gpu_requests
    ] | add == 0
  ' "$run_dir/node-pods-preflight.json" >/dev/null || {
    printf 'GPU preflight is not zero before %s\n' "$run_id" >&2
    exit 78
  }

  sed \
    -e "s|@@JOB_NAME@@|${run_id}|g" \
    -e "s|@@RUN_ID@@|${run_id}|g" \
    -e "s|@@INPUTS_NAME@@|${inputs_name}|g" \
    -e "s|@@TARGET_NODE@@|${allowed_node}|g" \
    -e "s|@@TARGET_IMAGE@@|${target_image}|g" \
    -e "s|@@CACHE_PVC@@|${cache_pvc}|g" \
    "$script_dir/conventional-job.yaml.tmpl" > "$run_dir/job.yaml"
  created_jobs+=("$run_id")
  date -u +%Y-%m-%dT%H:%M:%S.%NZ > "$run_dir/target-submit-at.txt"
  "${kubectl[@]}" create -f "$run_dir/job.yaml"
  pod=''
  for _ in $(seq 1 300); do
    pod=$("${kubectl[@]}" get pods -l "job-name=$run_id" -o json \
      | jq -er 'if (.items|length)==1 then .items[0].metadata.name else empty end' || true)
    [[ -n $pod ]] && break
    sleep 0.1
  done
  [[ -n $pod ]] || { printf 'conventional target Pod was not created\n' >&2; exit 1; }
  "${kubectl[@]}" wait --for=condition=Ready "pod/$pod" --timeout=120s
  "${kubectl[@]}" get pod "$pod" -o json > "$run_dir/pod-ready.json"
  "${kubectl[@]}" wait --for=condition=Complete "job/$run_id" --timeout=180s
  "${kubectl[@]}" get job "$run_id" -o json > "$run_dir/job.json"
  "${kubectl[@]}" get pods -l "job-name=$run_id" -o json > "$run_dir/pods.json"
  final_pod=$(jq -er 'select((.items|length)==1) | .items[0].metadata.name' "$run_dir/pods.json")
  [[ $final_pod == "$pod" ]] || { printf 'conventional target Pod identity changed\n' >&2; exit 1; }
  "${kubectl[@]}" get pod "$pod" -o json > "$run_dir/pod.json"
  pod_uid=$(jq -er '.metadata.uid' "$run_dir/pod.json")
  "${kubectl[@]}" get events --field-selector "involvedObject.uid=$pod_uid" -o json \
    > "$run_dir/target-events.json"
  jq -e --arg image "$target_image" --arg node "$allowed_node" '
    .spec.nodeName==$node and
    .status.phase=="Succeeded" and
    (.spec.containers|length)==1 and
    .spec.containers[0].image==$image and
    (.status.containerStatuses|length)==1 and
    .status.containerStatuses[0].restartCount==0 and
    (.status.containerStatuses[0].state.terminated.reason // "")=="Completed" and
    (.status.reason // "")!="Evicted"
  ' "$run_dir/pod.json" >/dev/null
  jq -e '[.items[] | select(.reason=="Pulling" or .reason=="BackOff" or .reason=="OOMKilling" or .reason=="Evicted")] | length==0' \
    "$run_dir/target-events.json" >/dev/null || {
      printf 'target image pull or terminal fault occurred after T0 for %s\n' "$run_id" >&2
      exit 1
    }
  "${kubectl[@]}" logs "$pod" > "$run_dir/pod.log"
  grep '^{' "$run_dir/pod.log" | jq -e -c \
    'select(.validator=="msa-search-pdb70-faststart-semantic-v1" and .status=="PASS" and .passed_case_count==2)' \
    > "$run_dir/semantic-summary.json"
  grep '^{' "$run_dir/pod.log" | jq -e -c \
    'select(.schema=="archvteams.nebius.ai/msa-search-mmseqs-pipe/v1" and .status=="PASS" and .shared_pipe_verified==true)' \
    > "$run_dir/mmseqs-pipe-receipt.json"

  python3 -B - "$run_dir/target-submit-at.txt" "$run_dir/semantic-summary.json" "$run_dir/pod-ready.json" > "$run_dir/result.json" <<'PY'
import datetime as dt
import json
import pathlib
import statistics
import sys

def stamp(value):
    return dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))

t0 = stamp(pathlib.Path(sys.argv[1]).read_text())
summary = json.loads(pathlib.Path(sys.argv[2]).read_text())
pod = json.loads(pathlib.Path(sys.argv[3]).read_text())
if (
    summary.get("response_timing_contract") != "request-dispatch-to-complete-http-body/v1"
    or summary.get("request_count") != 2
    or len(summary.get("cases", [])) != 2
    or summary.get("finished_at") != summary.get("validation_finished_at")
    or summary.get("total_elapsed_seconds")
    != summary.get("validation_total_elapsed_seconds")
):
    raise SystemExit("semantic summary lacks reviewed response-boundary provenance")
ready = stamp(summary["ready_at"])
call2_received = stamp(summary["cases"][1]["response_received_at"])
validation_finished = stamp(summary["validation_finished_at"])
if not ready <= stamp(summary["cases"][0]["request_started_at"]) <= stamp(
    summary["cases"][0]["response_received_at"]
) <= stamp(summary["cases"][1]["request_started_at"]) <= call2_received <= validation_finished:
    raise SystemExit("semantic response boundaries are not monotonically ordered")
k8s_ready = None
for condition in pod.get("status", {}).get("conditions", []):
    if condition.get("type") == "Ready" and condition.get("status") == "True":
        k8s_ready = stamp(condition["lastTransitionTime"])
        break
result = {
    "schema": "archvteams.nebius.ai/msa-search-conventional-trial/v1",
    "status": "PASS",
    "response_timing_contract": "request-dispatch-to-complete-http-body/v1",
    "run_id": summary["cases"][0]["input_id"].removesuffix("-semantic-a"),
    "storage_state": "cache PVC attached and fully prewarmed outside T0",
    "demand_at": t0.isoformat(),
    "http_ready_at": ready.isoformat(),
    "second_response_received_at": call2_received.isoformat(),
    "validation_finished_at": validation_finished.isoformat(),
    "demand_to_http_ready_seconds": round((ready - t0).total_seconds(), 6),
    "call1_seconds": summary["cases"][0]["elapsed_seconds"],
    "call2_seconds": summary["cases"][1]["elapsed_seconds"],
    "demand_to_call2_response_seconds": round((call2_received - t0).total_seconds(), 6),
    "kubernetes_ready_at": k8s_ready.isoformat() if k8s_ready else None,
    "demand_to_kubernetes_ready_seconds": round((k8s_ready - t0).total_seconds(), 6) if k8s_ready else None,
    "semantic_pass_count": 2,
    "mmseqs_pipe_verified": True,
}
print(json.dumps(result, sort_keys=True, separators=(",", ":")))
PY
  jq -e '.status=="PASS" and .semantic_pass_count==2 and .mmseqs_pipe_verified==true' "$run_dir/result.json" >/dev/null
  "${kubectl[@]}" delete job "$run_id" --wait=true --timeout=120s
  if "${kubectl[@]}" get job "$run_id" > "$run_dir/job-after-delete.txt" 2>&1; then
    printf 'Job remains after cleanup for %s\n' "$run_id" >&2
    exit 1
  fi
  for _ in $(seq 1 600); do
    "${kubectl[@]}" get pods -l "job-name=$run_id" -o json > "$run_dir/pods-after-delete.json"
    if jq -e '.items|length==0' "$run_dir/pods-after-delete.json" >/dev/null; then
      break
    fi
    sleep 0.1
  done
  jq -e '.items|length==0' "$run_dir/pods-after-delete.json" >/dev/null
  kubectl --kubeconfig "$kubeconfig" --context "$allowed_context" get pods --all-namespaces \
    --field-selector "spec.nodeName=$allowed_node" -o json > "$run_dir/node-pods-after-cleanup.json"
  post_cleanup_gpu_requests=$(jq '
    def gpu_requests:
      ([.spec.containers[]? | (.resources.requests["nvidia.com/gpu"] // "0" | tonumber)] | add // 0) as $app
      | ([.spec.initContainers[]? | (.resources.requests["nvidia.com/gpu"] // "0" | tonumber)] | if length==0 then 0 else max end) as $init
      | [$app, $init] | max;
    [ .items[]
      | select(.status.phase!="Succeeded" and .status.phase!="Failed")
      | gpu_requests
    ] | add // 0
  ' "$run_dir/node-pods-after-cleanup.json")
  [[ $post_cleanup_gpu_requests == 0 ]] || {
    printf 'GPU ownership is not zero after cleanup for %s\n' "$run_id" >&2
    exit 78
  }
  jq -n -c \
    --arg run_id "$run_id" \
    --arg verified_at "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" \
    --argjson active_gpu_requests "$post_cleanup_gpu_requests" \
    '{schema:"archvteams.nebius.ai/msa-search-conventional-cleanup/v1",status:"PASS",run_id:$run_id,job_absent:true,pod_count:0,active_gpu_requests_on_node:$active_gpu_requests,verified_at:$verified_at}' \
    > "$run_dir/cleanup-receipt.json"
done

python3 -B - "$evidence_root" "$batch_id" "$cache_pvc" > "$evidence_root/aggregate.json" <<'PY'
import datetime
import json
import pathlib
import statistics
import sys

root = pathlib.Path(sys.argv[1])
batch = sys.argv[2]
cache_pvc = sys.argv[3]
trials = [json.loads((root / f"{batch}-r{index}" / "result.json").read_text()) for index in (1, 2, 3)]
holder = json.loads((root / "cache-holder-receipt.json").read_text())
image = json.loads((root / "image-residency-receipt.json").read_text())
if (
    holder.get("status") != "PASS"
    or holder.get("storage_attached") is not True
    or holder.get("prewarm_outside_t0") is not True
):
    raise SystemExit("cache holder receipt is not a storage-attached prewarm PASS")
if (
    image.get("status") != "PASS"
    or image.get("preloaded_outside_t0") is not True
    or image.get("preloader_absent_after_setup") is not True
):
    raise SystemExit("target image residency receipt is not a pre-T0 PASS")
for trial in trials:
    if (
        trial.get("status") != "PASS"
        or trial.get("response_timing_contract")
        != "request-dispatch-to-complete-http-body/v1"
    ):
        raise SystemExit("trial lacks reviewed response-boundary provenance")
    demand_at = datetime.datetime.fromisoformat(trial["demand_at"])
    response_at = datetime.datetime.fromisoformat(trial["second_response_received_at"])
    validation_at = datetime.datetime.fromisoformat(trial["validation_finished_at"])
    if not demand_at <= response_at <= validation_at:
        raise SystemExit("trial response boundary is outside T0/validation")
    recomputed = round((response_at - demand_at).total_seconds(), 6)
    if trial.get("demand_to_call2_response_seconds") != recomputed:
        raise SystemExit("trial total does not match response boundary")
for index in (1, 2, 3):
    cleanup = json.loads((root / f"{batch}-r{index}" / "cleanup-receipt.json").read_text())
    if (
        cleanup.get("status") != "PASS"
        or cleanup.get("job_absent") is not True
        or cleanup.get("pod_count") != 0
        or cleanup.get("active_gpu_requests_on_node") != 0
    ):
        raise SystemExit(f"trial cleanup receipt is not a zero-GPU PASS: {index}")
fields = (
    "demand_to_http_ready_seconds",
    "call1_seconds",
    "call2_seconds",
    "demand_to_call2_response_seconds",
    "demand_to_kubernetes_ready_seconds",
)
metrics = {}
for field in fields:
    values = [trial[field] for trial in trials if trial[field] is not None]
    metrics[field] = {
        "values": values,
        "median": statistics.median(values) if values else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }
print(json.dumps({
    "schema": "archvteams.nebius.ai/msa-search-conventional-n3/v1",
    "status": "PASS",
    "response_timing_contract": "request-dispatch-to-complete-http-body/v1",
    "trial_count": 3,
    "semantic_call_count": 6,
    "storage_state": "cache PVC attached and fully prewarmed before every T0",
    "image_residency": image,
    "storage": {
        "pvc": cache_pvc,
        "attached_before_t0": True,
        "prewarm_outside_t0": True,
        "unique_regular_file_count": holder["regular_file_count"],
        "unique_prewarm_bytes": holder["prewarm_bytes"],
        "pdb70_index_bytes": holder["pdb70_index_bytes"],
        "content_stream_sha256": holder["content_stream_sha256"],
    },
    "metrics_seconds": metrics,
    "trials": trials,
}, sort_keys=True, separators=(",", ":")))
PY
jq -e '.status=="PASS" and .trial_count==3 and .semantic_call_count==6' "$evidence_root/aggregate.json" >/dev/null
cat "$evidence_root/aggregate.json"
