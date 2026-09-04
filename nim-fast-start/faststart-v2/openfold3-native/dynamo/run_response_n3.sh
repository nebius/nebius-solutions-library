#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

readonly allowed_server="${EXPECTED_API_SERVER:-https://kubernetes-api.example.invalid:443}"
readonly allowed_context="${EXPECTED_CONTEXT:-archvteams-2407-example}"
readonly allowed_node="${TARGET_NODE:-gpu-node-a.example.invalid}"
readonly namespace="${OPENFOLD3_NAMESPACE:-nim-fast-start}"
readonly holder_name="${OPENFOLD3_ARTIFACT_HOLDER:-openfold3-artifact-holder-example}"
readonly artifact_pvc="${OPENFOLD3_ARTIFACT_PVC:-openfold3-artifacts-example}"
readonly cache_pvc="${OPENFOLD3_CACHE_PVC:-openfold3-cache-example}"
readonly ngc_pull_secret="${OPENFOLD3_NGC_PULL_SECRET:-ngc-pull-secret-example}"
readonly worker_pull_secret="${OPENFOLD3_WORKER_PULL_SECRET:-worker-pull-secret-example}"
readonly checkpoint_id='openfold3-native-f7-v2-buffered'
readonly artifact_manifest_sha256='5df221e0736a4c6f369781ea0dbc7c36783c26d3f35dcd874b4ced8f5f9e009f'
readonly target_image='nvcr.io/nim/openfold/openfold3@sha256:6286cc7c02247ed3efe42f0f1af6c2f6f6a680b1e5cae669512c44b636aa42d2'
readonly expected_contract_sha256="${OPENFOLD3_CONTRACT_SHA256:-eb44b3e0c120e058415240aeb76b6c95d7a0f3ba01fc6dd8d2b66c23f05e166e}"
readonly expected_validator_sha256='679b3e027b18e78b4646569e8c6395fb5f62c4647704bb5089aa2385a20d11f5'
readonly expected_fixture_sha256='09b30bf2132e3764f99d4f417b47713cd6350bd332fe3100cceb1be11589f8ae'
readonly expected_prewarm_sha256='94fdabcae49fcde0fc5a3bddb2b4b75428995b1758015a98e0480ba9a1d36c82'
# Fresh qualification-lane capacity evidence proved that the original 1000m
# request left negative scheduling headroom. Keep the 4-CPU execution limit,
# but reserve the independently reviewed 500m so the cohort still retains at
# least 400m.
readonly worker_request_mcpu=500
readonly required_candidate_headroom_mcpu=400

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly script_dir
readonly lane_dir="${script_dir}/.."
readonly contract_path="${OPENFOLD3_CONTRACT_PATH:-${script_dir}/restore-interface.live.json}"
worker_image=$(jq -er '.worker_image' "$contract_path")
readonly worker_image
probe_image=$(jq -er '.probe_image' "$contract_path")
readonly probe_image

usage() {
  printf 'usage: %s --kubeconfig ABS_PATH --evidence-root ABS_DIR --batch-id DNS_LABEL [--preflight-only]\n' "$0" >&2
}

kubeconfig=''
evidence_root=''
batch_id=''
preflight_only=0
while (($#)); do
  case "$1" in
    --kubeconfig) kubeconfig=${2:-}; shift 2 ;;
    --evidence-root) evidence_root=${2:-}; shift 2 ;;
    --batch-id) batch_id=${2:-}; shift 2 ;;
    --preflight-only)
      ((preflight_only == 0)) || { usage; exit 64; }
      preflight_only=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) usage; exit 64 ;;
  esac
done
[[ $kubeconfig == /* && -f $kubeconfig && ! -L $kubeconfig ]] || { usage; exit 64; }
[[ $evidence_root == /* && ! -L $evidence_root ]] || { usage; exit 64; }
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
[[ $contract_path == /* && -f $contract_path && ! -L $contract_path ]] || {
  printf 'restore contract must be an absolute regular non-symlink file\n' >&2
  exit 64
}
for image in "$target_image" "$worker_image" "$probe_image"; do
  [[ $image =~ ^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$ ]] || {
    printf 'runtime image is not pinned by immutable digest\n' >&2
    exit 78
  }
done
for required in kubectl jq python3 sha256sum date install sed find; do
  command -v "$required" >/dev/null || { printf 'missing command: %s\n' "$required" >&2; exit 69; }
done

for local_receipt in \
  "$contract_path:$expected_contract_sha256" \
  "$lane_dir/validate_openfold3.py:$expected_validator_sha256" \
  "$lane_dir/fixtures/request-20aa.json:$expected_fixture_sha256" \
  "$lane_dir/prewarm_buffered_artifact.py:$expected_prewarm_sha256"; do
  local_path=${local_receipt%:*}
  local_expected=${local_receipt##*:}
  local_actual=$(sha256sum "$local_path")
  local_actual=${local_actual%% *}
  [[ $local_actual == "$local_expected" ]] || {
    printf 'local source digest mismatch: %s\n' "$local_path" >&2
    exit 78
  }
done

if [[ -e $evidence_root ]]; then
  [[ -d $evidence_root ]] || { printf 'evidence root is not a directory\n' >&2; exit 73; }
  [[ -z $(find "$evidence_root" -mindepth 1 -maxdepth 1 -print -quit) ]] || {
    printf 'evidence root must be new or empty\n' >&2
    exit 73
  }
fi
install -d -m 0700 -- "$evidence_root" "$evidence_root/runs" "$evidence_root/cohort-preflight"
readonly -a lane_kubectl=(kubectl --kubeconfig "$kubeconfig" --context "$allowed_context" -n "$namespace")
readonly -a cluster_kubectl=(kubectl --kubeconfig "$kubeconfig" --context "$allowed_context")
actual_context=$(kubectl --kubeconfig "$kubeconfig" config current-context)
[[ $actual_context == "$allowed_context" ]] || { printf 'kube context mismatch\n' >&2; exit 78; }
actual_server=$(kubectl --kubeconfig "$kubeconfig" --context "$allowed_context" config view --minify -o jsonpath='{.clusters[0].cluster.server}')
[[ $actual_server == "$allowed_server" ]] || { printf 'cluster identity mismatch\n' >&2; exit 78; }

preload_name="${batch_id}-images"
preload_created=0
active_run_id=''
finish() {
  local exit_code=$?
  trap - EXIT
  set +e
  if [[ -n $active_run_id ]]; then
    local active_dir="$evidence_root/runs/$active_run_id"
    for manifest in semantic-probe.yaml restore-worker.yaml target.yaml; do
      if [[ -f $active_dir/$manifest ]]; then
        "${lane_kubectl[@]}" delete -f "$active_dir/$manifest" \
          --ignore-not-found --wait=true --timeout=180s >/dev/null 2>&1
      fi
    done
  fi
  if ((preload_created == 1)); then
    "${lane_kubectl[@]}" delete pod "$preload_name" \
      --ignore-not-found --wait=true --timeout=120s >/dev/null 2>&1
  fi
  if ((exit_code != 0)); then
    jq -n -c --arg batch_id "$batch_id" --argjson exit_code "$exit_code" \
      --arg excluded_at "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" \
      '{schema:"archvteams.nebius.ai/openfold3-response-exclusion/v1",status:"EXCLUDED",batch_id:$batch_id,exit_code:$exit_code,excluded_at:$excluded_at,reason:"cohort did not complete three qualifying trials; no aggregate was admitted"}' \
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
for output in image-preload.yaml image-residency-receipt.json artifact-prewarm-receipt.json aggregate.json; do
  [[ ! -e $evidence_root/$output ]] || { printf 'cohort output already exists: %s\n' "$output" >&2; exit 73; }
done

"${cluster_kubectl[@]}" get node "$allowed_node" -o json > "$evidence_root/cohort-preflight/node.json"
"${cluster_kubectl[@]}" get pods -A --field-selector "spec.nodeName=$allowed_node" -o json \
  > "$evidence_root/cohort-preflight/node-pods.json"
"${lane_kubectl[@]}" get pod "$holder_name" -o json > "$evidence_root/cohort-preflight/holder.json"
"${lane_kubectl[@]}" get pvc "$artifact_pvc" "$cache_pvc" -o json \
  > "$evidence_root/cohort-preflight/pvcs.json"
"${cluster_kubectl[@]}" get volumeattachments.storage.k8s.io -o json \
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
  --arg checked_at "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" \
  --argjson worker_request "$worker_request_mcpu" \
  --argjson required_headroom "$required_candidate_headroom_mcpu" '
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
  ($allocatable_cpu-$cpu-10000-100-$worker_request) as $candidate_headroom |
  ([ $attachments.items[]|select(.spec.nodeName==$node_name and (.status.attached//false)==true and (.spec.source.persistentVolumeName==$artifact_pv or .spec.source.persistentVolumeName==$cache_pv)) ]) as $of3_vas |
  {
    schema:"archvteams.nebius.ai/openfold3-cohort-preflight/v1",
    status:(if any($n.status.conditions[]?;.type=="Ready" and .status=="True") and
      $n.metadata.labels["nebius.com/gpu-name"]=="H100" and
      $n.status.allocatable["nvidia.com/gpu"]=="1" and $gpu==0 and
      ($n.spec.unschedulable//false)==false and
      ([$n.spec.taints[]?|select(.effect=="NoSchedule" or .effect=="NoExecute")]|length)==0 and
      $candidate_headroom >= $required_headroom and
      $h.metadata.name==$holder_name and $h.spec.nodeName==$node_name and
      any($h.status.conditions[]?;.type=="Ready" and .status=="True") and
      (($h.status.containerStatuses//[])|length)>0 and
      all($h.status.containerStatuses[]?;.ready==true and .restartCount==0) and
      ([ $h.spec.volumes[]?|select(.persistentVolumeClaim)|.persistentVolumeClaim.claimName ]|sort)==([$artifact_pvc,$cache_pvc]|sort) and
      all($h.spec.volumes[]?|select(.persistentVolumeClaim);.persistentVolumeClaim.readOnly==true) and
      ($claims.items|length)==2 and all($claims.items[];.status.phase=="Bound") and
      ($of3_vas|length)==2 then "PASS" else "FAIL" end),
    checked_at:$checked_at,node:$node_name,active_gpu_requests_on_node:$gpu,
    allocatable_cpu_mcpu:$allocatable_cpu,active_existing_cpu_request_mcpu:$cpu,
    target_request_mcpu:10000,probe_request_mcpu:100,worker_request_mcpu:$worker_request,
    candidate_headroom_after_target_probe_worker_mcpu:$candidate_headroom,
    required_candidate_headroom_mcpu:$required_headroom,
    holder_uid:$h.metadata.uid,artifact_pv:$artifact_pv,cache_pv:$cache_pv,
    attached_volume_count:($of3_vas|length)
  }' > "$evidence_root/cohort-preflight/receipt.json"
if ! jq -e '.status=="PASS"' "$evidence_root/cohort-preflight/receipt.json" >/dev/null; then
  jq -c . "$evidence_root/cohort-preflight/receipt.json" >&2
  printf 'cohort preflight failed before mutation; retain this receipt for the CPU/storage audit\n' >&2
  exit 78
fi
if ((preflight_only == 1)); then
  jq -c . "$evidence_root/cohort-preflight/receipt.json"
  exit 0
fi

if "${lane_kubectl[@]}" get pod "$preload_name" >/dev/null 2>&1; then
  printf 'setup preloader already exists\n' >&2
  exit 78
fi
sed -e "s|@@PRELOAD_NAME@@|${preload_name}|g" -e "s|@@BATCH_ID@@|${batch_id}|g" \
  -e "s|@@NAMESPACE@@|${namespace}|g" -e "s|@@TARGET_NODE@@|${allowed_node}|g" \
  -e "s|@@TARGET_IMAGE@@|${target_image}|g" -e "s|@@WORKER_IMAGE@@|${worker_image}|g" \
  -e "s|@@PROBE_IMAGE@@|${probe_image}|g" \
  -e "s|@@NGC_PULL_SECRET@@|${ngc_pull_secret}|g" \
  -e "s|@@WORKER_PULL_SECRET@@|${worker_pull_secret}|g" \
  "$script_dir/image-preload.yaml.tmpl" > "$evidence_root/image-preload.yaml"
preload_created=1
"${lane_kubectl[@]}" create -f "$evidence_root/image-preload.yaml"
"${lane_kubectl[@]}" wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$preload_name" --timeout=900s
"${lane_kubectl[@]}" get pod "$preload_name" -o json > "$evidence_root/image-preload-pod.json"
preload_uid=$(jq -er '.metadata.uid' "$evidence_root/image-preload-pod.json")
"${lane_kubectl[@]}" get events --field-selector "involvedObject.uid=$preload_uid" -o json \
  > "$evidence_root/image-preload-events.json"
jq -e --arg target "$target_image" --arg worker "$worker_image" --arg probe "$probe_image" --arg node "$allowed_node" '
  .spec.nodeName==$node and .status.phase=="Succeeded" and
  ([.spec.containers[]|{key:.name,value:.image}]|from_entries)=={"target":$target,"restore-worker":$worker,"semantic-probe":$probe} and
  (.status.containerStatuses|length)==3 and
  all(.status.containerStatuses[];.restartCount==0 and .state.terminated.exitCode==0 and (.imageID|type=="string" and length>0)) and
  ([.spec.containers[]|(.resources.requests["nvidia.com/gpu"]//"0"|tonumber)]|add//0)==0
' "$evidence_root/image-preload-pod.json" >/dev/null
"${lane_kubectl[@]}" delete pod "$preload_name" --wait=true --timeout=120s
preload_created=0
if "${lane_kubectl[@]}" get pod "$preload_name" > "$evidence_root/image-preload-after-delete.txt" 2>&1; then
  printf 'setup image preloader remains after deletion\n' >&2
  exit 1
fi
jq -n -c --arg target "$target_image" --arg worker "$worker_image" --arg probe "$probe_image" \
  --arg target_id "$(jq -er '.status.containerStatuses[]|select(.name=="target")|.imageID' "$evidence_root/image-preload-pod.json")" \
  --arg worker_id "$(jq -er '.status.containerStatuses[]|select(.name=="restore-worker")|.imageID' "$evidence_root/image-preload-pod.json")" \
  --arg probe_id "$(jq -er '.status.containerStatuses[]|select(.name=="semantic-probe")|.imageID' "$evidence_root/image-preload-pod.json")" \
  --arg node "$allowed_node" --arg verified_at "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" \
  '{schema:"archvteams.nebius.ai/openfold3-image-residency/v1",status:"PASS",node:$node,preloaded_outside_t0:true,preloader_absent_before_t0:true,images:{target:$target,"restore-worker":$worker,"semantic-probe":$probe},image_ids:{target:$target_id,"restore-worker":$worker_id,"semantic-probe":$probe_id},verified_at:$verified_at}' \
  > "$evidence_root/image-residency-receipt.json"

"${lane_kubectl[@]}" get pod "$holder_name" -o json > "$evidence_root/artifact-holder-before-prewarm.json"
prewarm_started_at=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
"${lane_kubectl[@]}" exec -i "$holder_name" -- python3 - --source-node "$allowed_node" \
  < "$lane_dir/prewarm_buffered_artifact.py" \
  > "$evidence_root/artifact-prewarm-raw.json"
prewarm_completed_at=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
"${lane_kubectl[@]}" get pod "$holder_name" -o json > "$evidence_root/artifact-holder-after-prewarm.json"
jq -e --slurpfile before "$evidence_root/artifact-holder-before-prewarm.json" --slurpfile after "$evidence_root/artifact-holder-after-prewarm.json" '
  .schema=="archvteams.nebius.ai/openfold3-artifact-full-read/v1" and
  .status=="PASS" and .checkpoint_id=="openfold3-native-f7-v2-buffered" and
  .artifact_version=="1" and .image_io_mode=="buffered" and
  .regular_file_count==148 and .regular_bytes_read==9263246107 and
  .manifest_sha256=="5df221e0736a4c6f369781ea0dbc7c36783c26d3f35dcd874b4ced8f5f9e009f" and
  .tree_sha256=="f488019348551f356a153ce17cd9568a9d59497ead375c81a84ddef3bc3972c2" and
  ($before[0].metadata.uid==$after[0].metadata.uid) and
  any($after[0].status.conditions[]?;.type=="Ready" and .status=="True") and
  all($after[0].status.containerStatuses[]?;.ready==true and .restartCount==0)
' "$evidence_root/artifact-prewarm-raw.json" >/dev/null
jq -n -c --slurpfile raw "$evidence_root/artifact-prewarm-raw.json" \
  --arg holder_uid "$(jq -er '.metadata.uid' "$evidence_root/artifact-holder-after-prewarm.json")" \
  --arg source_sha256 "$expected_prewarm_sha256" --arg started_at "$prewarm_started_at" \
  --arg completed_at "$prewarm_completed_at" \
  '{schema:"archvteams.nebius.ai/openfold3-artifact-prewarm/v1",status:"PASS",checkpoint_id:$raw[0].checkpoint_id,artifact_version:$raw[0].artifact_version,image_io_mode:$raw[0].image_io_mode,source_node:$raw[0].source_node,regular_file_count:$raw[0].regular_file_count,regular_bytes_read:$raw[0].regular_bytes_read,manifest_sha256:$raw[0].manifest_sha256,tree_sha256:$raw[0].tree_sha256,full_read_elapsed_seconds:$raw[0].full_read_elapsed_seconds,prewarm_source_sha256:$source_sha256,holder_uid:$holder_uid,started_at:$started_at,completed_at:$completed_at,prewarm_outside_t0:true}' \
  > "$evidence_root/artifact-prewarm-receipt.json"

for run in 1 2 3; do
  active_run_id="${batch_id}-r${run}"
  run_dir="$evidence_root/runs/$active_run_id"
  "${cluster_kubectl[@]}" get pods -A --field-selector "spec.nodeName=$allowed_node" -o json \
    > "$evidence_root/node-pods-before-${active_run_id}.json"
  jq -e '
    def gpu_requests:
      ([.spec.containers[]?|(.resources.requests["nvidia.com/gpu"]//"0"|tonumber)]|add//0) as $app
      | ([.spec.initContainers[]?|(.resources.requests["nvidia.com/gpu"]//"0"|tonumber)]|if length==0 then 0 else max end) as $init
      | [$app,$init]|max;
    [.items[]|select(.status.phase!="Succeeded" and .status.phase!="Failed")|gpu_requests]|add==0
  ' "$evidence_root/node-pods-before-${active_run_id}.json" >/dev/null || {
    printf 'GPU preflight is not zero before %s\n' "$active_run_id" >&2
    exit 78
  }
  "$script_dir/run_provisioned_trial.sh" \
    --run-id "$active_run_id" --evidence-root "$evidence_root" --node "$allowed_node" \
    --kubeconfig "$kubeconfig" --artifact-holder "$holder_name" \
    --checkpoint-id "$checkpoint_id" --target-glibc-version 2.39 \
    --image-io-mode buffered --artifact-manifest-sha256 "$artifact_manifest_sha256" \
    --allow-performance-validation-worker --cleanup

  target_uid=$(jq -er '.target.uid' "$run_dir/canary-evidence.json")
  worker_uid=$(jq -er '.metadata.uid' "$run_dir/worker-pod.json")
  probe_uid=$(jq -er '.metadata.uid' "$run_dir/probe-pod.json")
  "${lane_kubectl[@]}" get events --field-selector "involvedObject.uid=$target_uid" -o json > "$run_dir/target-events.json"
  "${lane_kubectl[@]}" get events --field-selector "involvedObject.uid=$worker_uid" -o json > "$run_dir/worker-events.json"
  "${lane_kubectl[@]}" get events --field-selector "involvedObject.uid=$probe_uid" -o json > "$run_dir/probe-events.json"
  pulling_count=$(jq -s '[.[].items[]|select(.reason=="Pulling")]|length' "$run_dir/target-events.json" "$run_dir/worker-events.json" "$run_dir/probe-events.json")
  fault_count=$(jq -s '[.[].items[]|select(.reason=="BackOff" or .reason=="OOMKilling" or .reason=="Evicted" or .reason=="Failed")]|length' "$run_dir/target-events.json" "$run_dir/worker-events.json" "$run_dir/probe-events.json")
  [[ $pulling_count == 0 && $fault_count == 0 ]] || {
    printf 'post-T0 image pull or terminal fault event found for %s\n' "$active_run_id" >&2
    exit 1
  }
  jq -n -c --arg run_id "$active_run_id" --argjson pulling "$pulling_count" --argjson faults "$fault_count" \
    '{schema:"archvteams.nebius.ai/openfold3-trial-image-events/v1",status:"PASS",run_id:$run_id,pulling_event_count:$pulling,terminal_fault_event_count:$faults}' \
    > "$run_dir/image-events-receipt.json"

  "${lane_kubectl[@]}" get pods,jobs,services,configmaps,serviceaccounts,roles,rolebindings,networkpolicies \
    -l "archvteams.nebius.ai/run-id=$active_run_id" -o json > "$run_dir/run-resources-after-cleanup.json"
  "${lane_kubectl[@]}" get endpointslices.discovery.k8s.io -o json \
    > "$run_dir/endpoint-slices-after-cleanup.json"
  resource_count=$(jq -s '.[0].items|length' "$run_dir/run-resources-after-cleanup.json")
  endpoint_count=$(jq --arg canary "of3-canary-$active_run_id" \
    --arg qualified "of3-qualified-$active_run_id" \
    '[.items[]|select(.metadata.labels["kubernetes.io/service-name"]==$canary or .metadata.labels["kubernetes.io/service-name"]==$qualified)]|length' \
    "$run_dir/endpoint-slices-after-cleanup.json")
  resource_count=$((resource_count + endpoint_count))
  [[ $resource_count == 0 ]] || { printf 'run-scoped resources remain for %s\n' "$active_run_id" >&2; exit 1; }
  "${cluster_kubectl[@]}" get pods -A --field-selector "spec.nodeName=$allowed_node" -o json \
    > "$run_dir/node-pods-after-cleanup.json"
  active_gpu=$(jq '
    def gpu_requests:
      ([.spec.containers[]?|(.resources.requests["nvidia.com/gpu"]//"0"|tonumber)]|add//0) as $app
      | ([.spec.initContainers[]?|(.resources.requests["nvidia.com/gpu"]//"0"|tonumber)]|if length==0 then 0 else max end) as $init
      | [$app,$init]|max;
    [.items[]|select(.status.phase!="Succeeded" and .status.phase!="Failed")|gpu_requests]|add//0
  ' "$run_dir/node-pods-after-cleanup.json")
  [[ $active_gpu == 0 ]] || { printf 'GPU ownership is not zero after %s\n' "$active_run_id" >&2; exit 78; }
  jq -n -c --arg run_id "$active_run_id" --argjson count "$resource_count" --argjson gpu "$active_gpu" \
    --arg verified_at "$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)" \
    '{schema:"archvteams.nebius.ai/openfold3-cleanup/v1",status:"PASS",run_id:$run_id,run_scoped_resource_count:$count,active_gpu_requests_on_node:$gpu,verified_at:$verified_at}' \
    > "$run_dir/cleanup-receipt.json"
  active_run_id=''
done

python3 -B "$script_dir/aggregate_response_n3.py" --evidence-root "$evidence_root" --batch-id "$batch_id" \
  --node "$allowed_node" --target-image "$target_image" \
  --worker-image "$worker_image" --probe-image "$probe_image" \
  > "$evidence_root/aggregate.json"
jq -e '.status=="PASS" and .trial_count==3 and .semantic_call_count==6' "$evidence_root/aggregate.json" >/dev/null
jq -c . "$evidence_root/aggregate.json"
