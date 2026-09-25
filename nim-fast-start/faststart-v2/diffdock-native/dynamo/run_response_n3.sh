#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly allowed_server="${EXPECTED_API_SERVER:-https://kubernetes-api.example.invalid:443}"
readonly allowed_context="${EXPECTED_CONTEXT:-archvteams-2407-example}"
readonly allowed_node="${TARGET_NODE:-gpu-node-a.example.invalid}"
readonly namespace='nim-fast-start'
readonly holder_name="${DIFFDOCK_ARTIFACT_HOLDER:-diffdock-artifact-holder-example}"
readonly artifact_pvc="${DIFFDOCK_ARTIFACT_PVC:-diffdock-artifacts-example}"
readonly cache_pvc="${DIFFDOCK_CACHE_PVC:-diffdock-cache-example}"
readonly ngc_pull_secret="${DIFFDOCK_NGC_PULL_SECRET:-ngc-pull-secret-example}"
readonly worker_pull_secret="${DIFFDOCK_WORKER_PULL_SECRET:-worker-pull-secret-example}"
readonly checkpoint_id='diffdock-native-f7-v3-buffered'
readonly manifest_sha256='93a83188fb0adcc89c1278f136595c6dbce1b3fe9c412c3ccf65f704745ec1fe'
readonly target_image='nvcr.io/nim/mit/diffdock@sha256:300696eb8331d78face40f84d835cc1e278c7d3c391c5aabbbee5884366da480'
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly script_dir
readonly contract_path="${script_dir}/restore-interface.live.json"
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
[[ $evidence_root == /* && ! -e $evidence_root && ! -L $evidence_root ]] || { usage; exit 64; }
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

for required_command in kubectl jq python3 sed date install sha256sum sleep seq; do
  command -v "$required_command" >/dev/null || {
    printf 'required command is unavailable: %s\n' "$required_command" >&2
    exit 69
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
active_run=''

raw_delete_uid() {
  local uri=$1 uid=$2 prefix=$3
  jq -n --arg uid "$uid" \
    '{apiVersion:"v1",kind:"DeleteOptions",propagationPolicy:"Foreground",preconditions:{uid:$uid}}' \
    > "${prefix}.request.json"
  "${kubectl[@]}" delete --raw="$uri" -f "${prefix}.request.json" \
    > "${prefix}.response.json"
}

uid_cleanup_run() {
  local run_id=$1 run_dir=$2
  install -d -m 0700 -- "$run_dir"
  "${kubectl[@]}" get \
    pods,jobs,services,configmaps,serviceaccounts,roles,rolebindings,networkpolicies \
    -l "archvteams.nebius.ai/run-id=$run_id" -o json \
    > "$run_dir/resources-before-uid-cleanup.json"
  jq -e --arg run_id "$run_id" '
    all(.items[];
      .metadata.namespace=="nim-fast-start" and
      .metadata.labels["archvteams.nebius.ai/run-id"]==$run_id and
      (.metadata.uid|type=="string" and length>0))
  ' "$run_dir/resources-before-uid-cleanup.json" >/dev/null
  python3 - "$run_dir/resources-before-uid-cleanup.json" "$run_dir/uid-cleanup-targets.json" <<'PY'
import json
import sys
from pathlib import Path

source = json.loads(Path(sys.argv[1]).read_text())
mapping = {
    ("v1", "Pod"): (0, "/api/v1/namespaces/{ns}/pods/{name}"),
    ("batch/v1", "Job"): (1, "/apis/batch/v1/namespaces/{ns}/jobs/{name}"),
    ("v1", "Service"): (2, "/api/v1/namespaces/{ns}/services/{name}"),
    ("v1", "ConfigMap"): (3, "/api/v1/namespaces/{ns}/configmaps/{name}"),
    ("networking.k8s.io/v1", "NetworkPolicy"): (4, "/apis/networking.k8s.io/v1/namespaces/{ns}/networkpolicies/{name}"),
    ("rbac.authorization.k8s.io/v1", "RoleBinding"): (5, "/apis/rbac.authorization.k8s.io/v1/namespaces/{ns}/rolebindings/{name}"),
    ("rbac.authorization.k8s.io/v1", "Role"): (6, "/apis/rbac.authorization.k8s.io/v1/namespaces/{ns}/roles/{name}"),
    ("v1", "ServiceAccount"): (7, "/api/v1/namespaces/{ns}/serviceaccounts/{name}"),
}
targets = []
for item in source["items"]:
    key = (item.get("apiVersion"), item.get("kind"))
    if key not in mapping:
        raise SystemExit(f"unsupported cleanup kind: {key}")
    rank, template = mapping[key]
    meta = item["metadata"]
    targets.append({
        "rank": rank,
        "api_version": key[0],
        "kind": key[1],
        "name": meta["name"],
        "uid": meta["uid"],
        "uri": template.format(ns=meta["namespace"], name=meta["name"]),
    })
targets.sort(key=lambda value: (value["rank"], value["kind"], value["name"]))
Path(sys.argv[2]).write_text(json.dumps(targets, sort_keys=True, separators=(",", ":")) + "\n")
PY
  local uri uid kind name prefix index=0
  while IFS=$'\t' read -r uri uid kind name; do
    prefix="$run_dir/uid-delete-$(printf '%02d' "$index")-${kind}-${name}"
    raw_delete_uid "$uri" "$uid" "$prefix"
    index=$((index + 1))
  done < <(jq -r '.[]|[.uri,.uid,.kind,.name]|@tsv' "$run_dir/uid-cleanup-targets.json")

  local remaining=-1
  for _ in $(seq 1 180); do
    "${kubectl[@]}" get \
      pods,jobs,services,configmaps,serviceaccounts,roles,rolebindings,networkpolicies,endpointslices.discovery.k8s.io \
      -l "archvteams.nebius.ai/run-id=$run_id" -o json \
      > "$run_dir/resources-after-uid-cleanup.json"
    remaining=$(jq '.items|length' "$run_dir/resources-after-uid-cleanup.json")
    [[ $remaining == 0 ]] && break
    sleep 1
  done
  [[ $remaining == 0 ]] || { printf 'UID-scoped cleanup timed out for %s\n' "$run_id" >&2; return 1; }
  kubectl --kubeconfig "$kubeconfig" --context "$allowed_context" get pods -A \
    --field-selector "spec.nodeName=$allowed_node" -o json > "$run_dir/node-pods-after-cleanup.json"
  local active_gpu
  active_gpu=$(jq '
    def gpu_requests:
      ([.spec.containers[]?|(.resources.requests["nvidia.com/gpu"]//"0"|tonumber)]|add//0) as $app
      | ([.spec.initContainers[]?|(.resources.requests["nvidia.com/gpu"]//"0"|tonumber)]|if length==0 then 0 else max end) as $init
      | [$app,$init]|max;
    [.items[]|select(.status.phase!="Succeeded" and .status.phase!="Failed")|gpu_requests]|add//0
  ' "$run_dir/node-pods-after-cleanup.json")
  [[ $active_gpu == 0 ]] || { printf 'GPU ownership is not zero after %s\n' "$run_id" >&2; return 1; }
  jq -n -c --arg run_id "$run_id" --argjson count "$remaining" --argjson gpu "$active_gpu" \
    --slurpfile targets "$run_dir/uid-cleanup-targets.json" \
    --arg verified_at "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" \
    '{schema:"archvteams.nebius.ai/diffdock-cleanup/v1",status:"PASS",run_id:$run_id,uid_preconditions_enforced:true,deleted_objects:$targets[0],run_scoped_resource_count:$count,active_gpu_requests_on_node:$gpu,verified_at:$verified_at}' \
    > "$run_dir/cleanup-receipt.json"
}

finish() {
  local exit_code=$?
  trap - EXIT
  set +e
  if ((preload_created == 1)); then
    "${kubectl[@]}" delete pod "$preload_name" --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1
  fi
  if [[ -n $active_run ]]; then
    uid_cleanup_run "$active_run" "$evidence_root/runs/$active_run" >/dev/null 2>&1
  fi
  if ((exit_code != 0)); then
    jq -n -c --arg batch_id "$batch_id" --argjson exit_code "$exit_code" \
      --arg excluded_at "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" \
      '{schema:"archvteams.nebius.ai/diffdock-response-exclusion/v1",status:"EXCLUDED",batch_id:$batch_id,exit_code:$exit_code,excluded_at:$excluded_at,reason:"cohort did not complete three qualifying trials; no aggregate was admitted"}' \
      > "$evidence_root/EXCLUDED.json"
  fi
  exit "$exit_code"
}
trap finish EXIT

for run in 1 2 3; do
  [[ ! -e "$evidence_root/runs/${batch_id}-r${run}" ]] || exit 73
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
  ($node[0]) as $n | ($pods[0]) as $p | ($holder[0]) as $h |
  ($pvcs[0]) as $claims | ($vas[0]) as $attachments |
  ([ $p.items[]|select(.status.phase!="Succeeded" and .status.phase!="Failed")|gpu_requests ]|add//0) as $gpu |
  ([ $p.items[]|select(.status.phase!="Succeeded" and .status.phase!="Failed")|cpu_requests ]|add//0) as $cpu |
  ($n.status.allocatable.cpu|mcpu) as $allocatable_cpu |
  ($allocatable_cpu-$cpu-8000-100-1000) as $candidate_headroom |
  ([ $attachments.items[]|select(.spec.nodeName==$node_name and (.status.attached//false)==true and (.spec.source.persistentVolumeName==$artifact_pv or .spec.source.persistentVolumeName==$cache_pv)) ]) as $diffdock_vas |
  {
    schema:"archvteams.nebius.ai/diffdock-cohort-preflight/v1",
    status:(if any($n.status.conditions[]?;.type=="Ready" and .status=="True") and ($n.spec.taints//[]|length)==0 and $n.metadata.labels["node.kubernetes.io/instance-type"]=="gpu-h100-sxm" and $n.status.allocatable["nvidia.com/gpu"]=="1" and $gpu==0 and $candidate_headroom>=400 and any($p.items[]?;.metadata.namespace=="kube-system" and (.metadata.name|startswith("compute-csi-node-")) and any(.status.conditions[]?;.type=="Ready" and .status=="True") and all(.status.containerStatuses[]?;.ready==true)) and $h.metadata.name==$holder_name and $h.spec.nodeName==$node_name and any($h.status.conditions[]?;.type=="Ready" and .status=="True") and all($h.status.containerStatuses[]?;.ready==true and .restartCount==0) and ([ $h.spec.volumes[]?|select(.persistentVolumeClaim)|.persistentVolumeClaim.claimName ]|sort)==([$artifact_pvc,$cache_pvc]|sort) and ($claims.items|length)==2 and all($claims.items[];.status.phase=="Bound") and ($diffdock_vas|length)==2 then "PASS" else "FAIL" end),
    checked_at:$checked_at,node:$node_name,active_gpu_requests_on_node:$gpu,
    allocatable_cpu_mcpu:$allocatable_cpu,active_existing_cpu_request_mcpu:$cpu,
    target_cpu_request_mcpu:8000,probe_cpu_request_mcpu:100,
    worker_cpu_request_mcpu:1000,worker_cpu_limit_mcpu:4000,
    candidate_headroom_mcpu:$candidate_headroom,required_candidate_headroom_mcpu:400,
    holder_uid:$h.metadata.uid,artifact_pv:$artifact_pv,cache_pv:$cache_pv,
    attached_volume_count:($diffdock_vas|length),csi_node_ready:true
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
raw_delete_uid "/api/v1/namespaces/$namespace/pods/$preload_name" "$preload_uid" "$evidence_root/image-preload-uid-delete"
preload_created=0
for _ in $(seq 1 120); do
  ! "${kubectl[@]}" get pod "$preload_name" > "$evidence_root/image-preload-after-delete.txt" 2>&1 && break
  sleep 1
done
if "${kubectl[@]}" get pod "$preload_name" >/dev/null 2>&1; then
  printf 'setup image preloader remains after UID-scoped deletion\n' >&2
  exit 1
fi
jq -n -c \
  --arg target "$target_image" --arg worker "$worker_image" --arg probe "$probe_image" \
  --arg target_id "$(jq -er '.status.containerStatuses[]|select(.name=="target")|.imageID' "$evidence_root/image-preload-pod.json")" \
  --arg worker_id "$(jq -er '.status.containerStatuses[]|select(.name=="restore-worker")|.imageID' "$evidence_root/image-preload-pod.json")" \
  --arg probe_id "$(jq -er '.status.containerStatuses[]|select(.name=="semantic-probe")|.imageID' "$evidence_root/image-preload-pod.json")" \
  --arg node "$allowed_node" --arg verified_at "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" \
  '{schema:"archvteams.nebius.ai/diffdock-image-residency/v1",status:"PASS",node:$node,preloaded_outside_t0:true,preloader_absent_before_t0:true,preloader_uid_delete_precondition:true,images:{target:$target,"restore-worker":$worker,"semantic-probe":$probe},image_ids:{target:$target_id,"restore-worker":$worker_id,"semantic-probe":$probe_id},verified_at:$verified_at}' \
  > "$evidence_root/image-residency-receipt.json"

# Final artifact setup operation before the cohort. The script reads and hashes
# every logical artifact byte through buffered I/O inside the unchanged holder.
"${kubectl[@]}" get pod "$holder_name" -o json > "$evidence_root/artifact-holder-before-prewarm.json"
prewarm_started_at=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
"${kubectl[@]}" exec -i "$holder_name" -- python3 - < "$script_dir/../prewarm_buffered_artifact.py" \
  > "$evidence_root/artifact-prewarm-raw.json"
prewarm_completed_at=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
"${kubectl[@]}" get pod "$holder_name" -o json > "$evidence_root/artifact-holder-after-prewarm.json"
jq -e --slurpfile before "$evidence_root/artifact-holder-before-prewarm.json" --slurpfile after "$evidence_root/artifact-holder-after-prewarm.json" '
  .status=="PASS" and .checkpoint_id=="diffdock-native-f7-v3-buffered" and .version=="1" and
  .image_io_mode=="buffered" and .regular_file_count==122 and .regular_bytes_read==7516058314 and
  .manifest_sha256=="93a83188fb0adcc89c1278f136595c6dbce1b3fe9c412c3ccf65f704745ec1fe" and
  .aggregate_content_sha256=="2d9e339392d6b4c5207ddbd4ef8f26465e324b2e165bd4cd9b43530f006e1b1d" and
  ($before[0].metadata.uid==$after[0].metadata.uid) and
  any($after[0].status.conditions[]?;.type=="Ready" and .status=="True") and
  all($after[0].status.containerStatuses[]?;.ready==true and .restartCount==0)
' "$evidence_root/artifact-prewarm-raw.json" >/dev/null
jq -n -c --slurpfile raw "$evidence_root/artifact-prewarm-raw.json" \
  --arg holder_uid "$(jq -er '.metadata.uid' "$evidence_root/artifact-holder-after-prewarm.json")" \
  --arg started_at "$prewarm_started_at" --arg completed_at "$prewarm_completed_at" \
  '{schema:"archvteams.nebius.ai/diffdock-artifact-prewarm/v1",status:"PASS",checkpoint_id:$raw[0].checkpoint_id,artifact_version:$raw[0].version,image_io_mode:$raw[0].image_io_mode,regular_file_count:$raw[0].regular_file_count,regular_bytes_read:$raw[0].regular_bytes_read,manifest_sha256:$raw[0].manifest_sha256,tree_sha256:$raw[0].aggregate_content_sha256,full_read_elapsed_seconds:$raw[0].elapsed_seconds,holder_uid:$holder_uid,started_at:$started_at,completed_at:$completed_at,prewarm_outside_t0:true,perturbing_artifact_setup_after_prewarm:0}' \
  > "$evidence_root/artifact-prewarm-receipt.json"

for run in 1 2 3; do
  run_id="${batch_id}-r${run}"
  run_dir="$evidence_root/runs/$run_id"
  active_run=$run_id
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
    --kubeconfig "$kubeconfig" --artifact-holder "$holder_name" \
    --checkpoint-id "$checkpoint_id" --target-glibc-version 2.35 \
    --artifact-manifest-sha256 "$manifest_sha256"

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
    '{schema:"archvteams.nebius.ai/diffdock-trial-image-events/v1",status:"PASS",run_id:$run_id,pulling_event_count:$pulling,terminal_fault_event_count:$faults}' \
    > "$run_dir/image-events-receipt.json"
  uid_cleanup_run "$run_id" "$run_dir"
  active_run=''
done

python3 -B "$script_dir/aggregate_response_n3.py" \
  --evidence-root "$evidence_root" --batch-id "$batch_id" \
  --node "$allowed_node" --target-image "$target_image" \
  --worker-image "$worker_image" --probe-image "$probe_image" \
  > "$evidence_root/aggregate.json"
jq -e '.status=="PASS" and .trial_count==3 and .semantic_call_count==6' "$evidence_root/aggregate.json" >/dev/null
trap - EXIT
jq -c . "$evidence_root/aggregate.json"
