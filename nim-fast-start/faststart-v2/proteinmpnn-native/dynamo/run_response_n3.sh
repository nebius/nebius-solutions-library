#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly allowed_server="${EXPECTED_API_SERVER:-https://kubernetes-api.example.invalid:443}"
readonly allowed_context="${EXPECTED_CONTEXT:-archvteams-2407-example}"
readonly allowed_node="${TARGET_NODE:-gpu-node-a.example.invalid}"
readonly namespace='nim-fast-start'
readonly holder_name="${PMP_ARTIFACT_HOLDER:-proteinmpnn-artifact-holder-example}"
readonly artifact_pvc="${PMP_ARTIFACT_PVC:-proteinmpnn-artifacts-example}"
readonly cache_pvc="${PMP_CACHE_PVC:-proteinmpnn-cache-example}"
readonly ngc_pull_secret="${PMP_NGC_PULL_SECRET:-ngc-pull-secret-example}"
readonly worker_pull_secret="${PMP_WORKER_PULL_SECRET:-worker-pull-secret-example}"
readonly target_image='nvcr.io/nim/ipd/proteinmpnn@sha256:b55a0aa6733e267e6e6fe06434e98aea61eff14bc5545127555607fef6f38aa5'
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly script_dir
readonly contract_path="${PMP_CONTRACT_PATH:-${script_dir}/restore-interface.live.json}"
worker_image=$(jq -er '.worker_image' "$contract_path")
readonly worker_image
probe_image=$(jq -er '.probe_image' "$contract_path")
readonly probe_image

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
[[ $batch_id =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#batch_id} -le 25 ]] || { usage; exit 64; }
for dns_label in "$namespace" "$holder_name" "$artifact_pvc" "$cache_pvc" \
  "$ngc_pull_secret" "$worker_pull_secret"; do
  [[ $dns_label =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ && ${#dns_label} -le 63 ]] || {
    printf 'runtime Kubernetes identity is not a DNS label: %s\n' "$dns_label" >&2
    exit 64
  }
done
[[ $allowed_node =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ && ${#allowed_node} -le 253 ]] || {
  printf 'target node is not a DNS subdomain\n' >&2
  exit 64
}
for image in "$target_image" "$worker_image" "$probe_image"; do
  [[ $image =~ ^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$ ]] || {
    printf 'runtime image is not pinned by immutable digest\n' >&2
    exit 78
  }
done

install -d -m 0700 -- "$evidence_root" "$evidence_root/runs" "$evidence_root/cohort-preflight"
readonly -a kubectl=(kubectl --kubeconfig "$kubeconfig" --context "$allowed_context" -n "$namespace")
actual_context=$(kubectl --kubeconfig "$kubeconfig" config current-context)
[[ $actual_context == "$allowed_context" ]] || { printf 'kube context mismatch\n' >&2; exit 78; }
actual_server=$(kubectl --kubeconfig "$kubeconfig" --context "$allowed_context" config view --minify -o jsonpath='{.clusters[0].cluster.server}')
[[ $actual_server == "$allowed_server" ]] || { printf 'cluster identity mismatch\n' >&2; exit 78; }

preload_name="${batch_id}-images"
preload_created=0
finish() {
  local exit_code=$?
  trap - EXIT
  set +e
  if ((preload_created == 1)); then
    "${kubectl[@]}" delete pod "$preload_name" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1
  fi
  if ((exit_code != 0)); then
    jq -n -c \
      --arg batch_id "$batch_id" \
      --argjson exit_code "$exit_code" \
      --arg excluded_at "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" \
      '{schema:"archvteams.nebius.ai/proteinmpnn-response-exclusion/v1",status:"EXCLUDED",batch_id:$batch_id,exit_code:$exit_code,excluded_at:$excluded_at,reason:"cohort did not complete three qualifying trials; no aggregate was admitted"}' \
      > "$evidence_root/EXCLUDED.json"
  fi
  exit "$exit_code"
}
trap finish EXIT

for run in 1 2 3; do
  [[ ! -e "$evidence_root/runs/${batch_id}-r${run}" ]] || {
    printf 'run evidence already exists: %s-r%s\n' "$batch_id" "$run" >&2
    exit 73
  }
done
if "${kubectl[@]}" get pod "$preload_name" >/dev/null 2>&1; then
  printf 'setup preloader already exists\n' >&2
  exit 78
fi

"${kubectl[@]}" get node "$allowed_node" -o json > "$evidence_root/cohort-preflight/node.json"
kubectl --kubeconfig "$kubeconfig" --context "$allowed_context" get pods -A \
  --field-selector "spec.nodeName=$allowed_node" -o json > "$evidence_root/cohort-preflight/node-pods.json"
"${kubectl[@]}" get pod "$holder_name" -o json > "$evidence_root/cohort-preflight/holder.json"
"${kubectl[@]}" get pvc "$artifact_pvc" "$cache_pvc" -o json > "$evidence_root/cohort-preflight/pvcs.json"
kubectl --kubeconfig "$kubeconfig" --context "$allowed_context" get volumeattachments.storage.k8s.io -o json \
  > "$evidence_root/cohort-preflight/volumeattachments.json"
artifact_pv=$(jq -er --arg claim "$artifact_pvc" '.items[]|select(.metadata.name==$claim)|.spec.volumeName' "$evidence_root/cohort-preflight/pvcs.json")
cache_pv=$(jq -er --arg claim "$cache_pvc" '.items[]|select(.metadata.name==$claim)|.spec.volumeName' "$evidence_root/cohort-preflight/pvcs.json")
jq -n -c \
  --slurpfile node "$evidence_root/cohort-preflight/node.json" \
  --slurpfile pods "$evidence_root/cohort-preflight/node-pods.json" \
  --slurpfile holder "$evidence_root/cohort-preflight/holder.json" \
  --slurpfile pvcs "$evidence_root/cohort-preflight/pvcs.json" \
  --slurpfile vas "$evidence_root/cohort-preflight/volumeattachments.json" \
  --arg node_name "$allowed_node" --arg holder_name "$holder_name" \
  --arg artifact_pvc "$artifact_pvc" --arg cache_pvc "$cache_pvc" \
  --arg artifact_pv "$artifact_pv" --arg cache_pv "$cache_pv" \
  --arg checked_at "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" '
  def mcpu:
    tostring as $value |
    if ($value|endswith("m")) then ($value[:-1]|tonumber)
    elif ($value|endswith("u")) then (($value[:-1]|tonumber)/1000)
    elif ($value|endswith("n")) then (($value[:-1]|tonumber)/1000000)
    else (($value|tonumber)*1000) end;
  def gpu_requests:
    ([.spec.containers[]?|(.resources.requests["nvidia.com/gpu"]//"0"|tonumber)]|add//0) as $app
    | ([.spec.initContainers[]?|(.resources.requests["nvidia.com/gpu"]//"0"|tonumber)]|if length==0 then 0 else max end) as $init
    | [$app,$init]|max;
  def cpu_requests:
    ([.spec.containers[]?|(.resources.requests.cpu//"0"|mcpu)]|add//0) as $app
    | ([.spec.initContainers[]?|(.resources.requests.cpu//"0"|mcpu)]|if length==0 then 0 else max end) as $init
    | (.spec.overhead.cpu//"0"|mcpu) as $overhead
    | ([$app,$init]|max)+$overhead;
  ($node[0]) as $n | ($pods[0]) as $p | ($holder[0]) as $h | ($pvcs[0]) as $claims | ($vas[0]) as $attachments |
  ([ $p.items[]|select(.status.phase!="Succeeded" and .status.phase!="Failed")|gpu_requests ]|add//0) as $gpu |
  ([ $p.items[]|select(.status.phase!="Succeeded" and .status.phase!="Failed")|cpu_requests ]|add//0) as $cpu |
  ($n.status.allocatable.cpu|mcpu) as $allocatable_cpu |
  ($allocatable_cpu-$cpu-10000-100-500) as $candidate_headroom |
  ([ $attachments.items[]|select(.spec.nodeName==$node_name and (.status.attached//false)==true and (.spec.source.persistentVolumeName==$artifact_pv or .spec.source.persistentVolumeName==$cache_pv)) ]) as $pmp_vas |
  {
    schema:"archvteams.nebius.ai/proteinmpnn-cohort-preflight/v1",
    status:(if any($n.status.conditions[]?;.type=="Ready" and .status=="True") and $n.status.allocatable["nvidia.com/gpu"]=="1" and $gpu==0 and $cpu==4830 and $candidate_headroom>=400 and $h.metadata.name==$holder_name and $h.spec.nodeName==$node_name and any($h.status.conditions[]?;.type=="Ready" and .status=="True") and ([ $h.spec.volumes[]?|select(.persistentVolumeClaim)|.persistentVolumeClaim.claimName ]|sort)==([$artifact_pvc,$cache_pvc]|sort) and ($claims.items|length)==2 and all($claims.items[];.status.phase=="Bound") and ($pmp_vas|length)==2 then "PASS" else "FAIL" end),
    checked_at:$checked_at,node:$node_name,active_gpu_requests_on_node:$gpu,
    allocatable_cpu_mcpu:$allocatable_cpu,active_existing_cpu_request_mcpu:$cpu,
    candidate_headroom_after_target_probe_worker_mcpu:$candidate_headroom,
    required_candidate_headroom_mcpu:400,
    holder_uid:$h.metadata.uid,artifact_pv:$artifact_pv,cache_pv:$cache_pv,
    attached_volume_count:($pmp_vas|length)
  }' > "$evidence_root/cohort-preflight/receipt.json"
jq -e '.status=="PASS"' "$evidence_root/cohort-preflight/receipt.json" >/dev/null

sed -e "s|@@PRELOAD_NAME@@|${preload_name}|g" -e "s|@@BATCH_ID@@|${batch_id}|g" \
  -e "s|@@TARGET_NODE@@|${allowed_node}|g" \
  -e "s|@@TARGET_IMAGE@@|${target_image}|g" \
  -e "s|@@WORKER_IMAGE@@|${worker_image}|g" \
  -e "s|@@PROBE_IMAGE@@|${probe_image}|g" \
  -e "s|@@NGC_PULL_SECRET@@|${ngc_pull_secret}|g" \
  -e "s|@@WORKER_PULL_SECRET@@|${worker_pull_secret}|g" \
  "$script_dir/image-preload.yaml.tmpl" > "$evidence_root/image-preload.yaml"
preload_created=1
"${kubectl[@]}" create -f "$evidence_root/image-preload.yaml"
"${kubectl[@]}" wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$preload_name" --timeout=600s
"${kubectl[@]}" get pod "$preload_name" -o json > "$evidence_root/image-preload-pod.json"
preload_uid=$(jq -er '.metadata.uid' "$evidence_root/image-preload-pod.json")
"${kubectl[@]}" get events --field-selector "involvedObject.uid=$preload_uid" -o json > "$evidence_root/image-preload-events.json"
jq -e --arg target "$target_image" --arg worker "$worker_image" --arg probe "$probe_image" --arg node "$allowed_node" '
  .spec.nodeName==$node and .status.phase=="Succeeded" and
  ([.spec.containers[]|{key:.name,value:.image}]|from_entries)=={"target":$target,"restore-worker":$worker,"semantic-probe":$probe} and
  (.status.containerStatuses|length)==3 and
  all(.status.containerStatuses[];.restartCount==0 and .state.terminated.exitCode==0 and (.imageID|type=="string" and length>0)) and
  ([.spec.containers[]|(.resources.requests["nvidia.com/gpu"]//"0"|tonumber)]|add//0)==0
' "$evidence_root/image-preload-pod.json" >/dev/null
"${kubectl[@]}" delete pod "$preload_name" --wait=true --timeout=120s
preload_created=0
if "${kubectl[@]}" get pod "$preload_name" > "$evidence_root/image-preload-after-delete.txt" 2>&1; then
  printf 'setup image preloader remains after deletion\n' >&2
  exit 1
fi
jq -n -c \
  --arg target "$target_image" --arg worker "$worker_image" --arg probe "$probe_image" \
  --arg target_id "$(jq -er '.status.containerStatuses[]|select(.name=="target")|.imageID' "$evidence_root/image-preload-pod.json")" \
  --arg worker_id "$(jq -er '.status.containerStatuses[]|select(.name=="restore-worker")|.imageID' "$evidence_root/image-preload-pod.json")" \
  --arg probe_id "$(jq -er '.status.containerStatuses[]|select(.name=="semantic-probe")|.imageID' "$evidence_root/image-preload-pod.json")" \
  --arg node "$allowed_node" --arg verified_at "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" \
  '{schema:"archvteams.nebius.ai/proteinmpnn-image-residency/v1",status:"PASS",node:$node,preloaded_outside_t0:true,preloader_absent_before_t0:true,images:{target:$target,"restore-worker":$worker,"semantic-probe":$probe},image_ids:{target:$target_id,"restore-worker":$worker_id,"semantic-probe":$probe_id},verified_at:$verified_at}' \
  > "$evidence_root/image-residency-receipt.json"

# This is the final artifact setup operation before the cohort. Every byte is
# read through buffered I/O in the existing same-node holder, outside T0.
"${kubectl[@]}" get pod "$holder_name" -o json > "$evidence_root/artifact-holder-before-prewarm.json"
prewarm_started_at=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
"${kubectl[@]}" exec -i "$holder_name" -- python3 - < "$script_dir/../prewarm_buffered_artifact.py" \
  > "$evidence_root/artifact-prewarm-raw.json"
prewarm_completed_at=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
"${kubectl[@]}" get pod "$holder_name" -o json > "$evidence_root/artifact-holder-after-prewarm.json"
jq -e --slurpfile before "$evidence_root/artifact-holder-before-prewarm.json" --slurpfile after "$evidence_root/artifact-holder-after-prewarm.json" '
  .status=="PASS" and .checkpoint_id=="proteinmpnn-native-f7-v3-buffered" and .version=="1" and
  .regular_file_count==57 and .regular_bytes_read==1867046505 and
  .manifest_sha256=="6a298ceefc93b259e5ec7e6c1e74ae3ab43cdd9a757bee1934923dbfcdc06c07" and
  ($before[0].metadata.uid==$after[0].metadata.uid) and
  any($after[0].status.conditions[]?;.type=="Ready" and .status=="True") and
  all($after[0].status.containerStatuses[]?;.restartCount==0)
' "$evidence_root/artifact-prewarm-raw.json" >/dev/null
jq -n -c \
  --slurpfile raw "$evidence_root/artifact-prewarm-raw.json" \
  --arg holder_uid "$(jq -er '.metadata.uid' "$evidence_root/artifact-holder-after-prewarm.json")" \
  --arg started_at "$prewarm_started_at" --arg completed_at "$prewarm_completed_at" \
  '{schema:"archvteams.nebius.ai/proteinmpnn-artifact-prewarm/v1",status:"PASS",checkpoint_id:$raw[0].checkpoint_id,artifact_version:$raw[0].version,regular_file_count:$raw[0].regular_file_count,regular_bytes_read:$raw[0].regular_bytes_read,manifest_sha256:$raw[0].manifest_sha256,tree_sha256:$raw[0].aggregate_content_sha256,full_read_elapsed_seconds:$raw[0].elapsed_seconds,holder_uid:$holder_uid,started_at:$started_at,completed_at:$completed_at,prewarm_outside_t0:true}' \
  > "$evidence_root/artifact-prewarm-receipt.json"

for run in 1 2 3; do
  run_id="${batch_id}-r${run}"
  run_dir="$evidence_root/runs/$run_id"
  kubectl --kubeconfig "$kubeconfig" --context "$allowed_context" get pods -A \
    --field-selector "spec.nodeName=$allowed_node" -o json > "$evidence_root/node-pods-before-${run_id}.json"
  jq -e '
    def gpu_requests:
      ([.spec.containers[]?|(.resources.requests["nvidia.com/gpu"]//"0"|tonumber)]|add//0) as $app
      | ([.spec.initContainers[]?|(.resources.requests["nvidia.com/gpu"]//"0"|tonumber)]|if length==0 then 0 else max end) as $init
      | [$app,$init]|max;
    [.items[]|select(.status.phase!="Succeeded" and .status.phase!="Failed")|gpu_requests]|add==0
  ' "$evidence_root/node-pods-before-${run_id}.json" >/dev/null || {
    printf 'GPU preflight is not zero before %s\n' "$run_id" >&2
    exit 78
  }
  "$script_dir/run_provisioned_trial.sh" \
    --run-id "$run_id" --evidence-root "$evidence_root" --node "$allowed_node" \
    --kubeconfig "$kubeconfig" --artifact-holder "$holder_name" --cleanup

  target_uid=$(jq -er '.target.uid' "$run_dir/canary-evidence.json")
  worker_uid=$(jq -er '.metadata.uid' "$run_dir/worker-pod.json")
  probe_uid=$(jq -er '.metadata.uid' "$run_dir/probe-pod.json")
  "${kubectl[@]}" get events --field-selector "involvedObject.uid=$target_uid" -o json > "$run_dir/target-events.json"
  "${kubectl[@]}" get events --field-selector "involvedObject.uid=$worker_uid" -o json > "$run_dir/worker-events.json"
  "${kubectl[@]}" get events --field-selector "involvedObject.uid=$probe_uid" -o json > "$run_dir/probe-events.json"
  pulling_count=$(jq -s '[.[].items[]|select(.reason=="Pulling")]|length' "$run_dir/target-events.json" "$run_dir/worker-events.json" "$run_dir/probe-events.json")
  fault_count=$(jq -s '[.[].items[]|select(.reason=="BackOff" or .reason=="OOMKilling" or .reason=="Evicted" or .reason=="Failed")]|length' "$run_dir/target-events.json" "$run_dir/worker-events.json" "$run_dir/probe-events.json")
  [[ $pulling_count == 0 && $fault_count == 0 ]] || {
    printf 'post-T0 image pull or terminal fault event found for %s\n' "$run_id" >&2
    exit 1
  }
  jq -n -c --arg run_id "$run_id" --argjson pulling "$pulling_count" --argjson faults "$fault_count" \
    '{schema:"archvteams.nebius.ai/proteinmpnn-trial-image-events/v1",status:"PASS",run_id:$run_id,pulling_event_count:$pulling,terminal_fault_event_count:$faults}' \
    > "$run_dir/image-events-receipt.json"

  "${kubectl[@]}" get pods,jobs,services,configmaps,serviceaccounts,roles,rolebindings,networkpolicies \
    -l "archvteams.nebius.ai/run-id=$run_id" -o json > "$run_dir/run-resources-after-cleanup.json"
  resource_count=$(jq '.items|length' "$run_dir/run-resources-after-cleanup.json")
  [[ $resource_count == 0 ]] || { printf 'run-scoped resources remain for %s\n' "$run_id" >&2; exit 1; }
  kubectl --kubeconfig "$kubeconfig" --context "$allowed_context" get pods -A \
    --field-selector "spec.nodeName=$allowed_node" -o json > "$run_dir/node-pods-after-cleanup.json"
  active_gpu=$(jq '
    def gpu_requests:
      ([.spec.containers[]?|(.resources.requests["nvidia.com/gpu"]//"0"|tonumber)]|add//0) as $app
      | ([.spec.initContainers[]?|(.resources.requests["nvidia.com/gpu"]//"0"|tonumber)]|if length==0 then 0 else max end) as $init
      | [$app,$init]|max;
    [.items[]|select(.status.phase!="Succeeded" and .status.phase!="Failed")|gpu_requests]|add//0
  ' "$run_dir/node-pods-after-cleanup.json")
  [[ $active_gpu == 0 ]] || { printf 'GPU ownership is not zero after %s\n' "$run_id" >&2; exit 78; }
  jq -n -c --arg run_id "$run_id" --argjson count "$resource_count" --argjson gpu "$active_gpu" \
    --arg verified_at "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" \
    '{schema:"archvteams.nebius.ai/proteinmpnn-cleanup/v1",status:"PASS",run_id:$run_id,run_scoped_resource_count:$count,active_gpu_requests_on_node:$gpu,verified_at:$verified_at}' \
    > "$run_dir/cleanup-receipt.json"
done

python3 -B "$script_dir/aggregate_response_n3.py" --evidence-root "$evidence_root" --batch-id "$batch_id" \
  --node "$allowed_node" --target-image "$target_image" \
  --worker-image "$worker_image" --probe-image "$probe_image" \
  > "$evidence_root/aggregate.json"
jq -e '.status=="PASS" and .trial_count==3 and .semantic_call_count==6' "$evidence_root/aggregate.json" >/dev/null
jq -c . "$evidence_root/aggregate.json"
