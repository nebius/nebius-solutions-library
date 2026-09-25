#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# -ne 2 || $2 != --execute ]]; then
  echo "usage: $0 RUN_ID --execute" >&2
  exit 64
fi
if [[ ${OPENFOLD2_NEWNODE_COORDINATED:-} != YES ]]; then
  echo "refusing: set OPENFOLD2_NEWNODE_COORDINATED=YES only after the explicit live handoff" >&2
  exit 78
fi

run_id=$1
if [[ ! $run_id =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ || ${#run_id} -gt 30 ]]; then
  echo "invalid run ID" >&2
  exit 64
fi

harness_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
if [[ -d $harness_root/../dynamo ]]; then
  pipeline_root=$(cd -- "$harness_root/../dynamo" && pwd -P)
else
  pipeline_root=$harness_root/frozen/faststart-v2/dynamo
fi
runtime_pipeline=$harness_root/runtime_pipeline.py
node_admission=$harness_root/node_admission.py
node_service_gate=$harness_root/node_service_gate.jq
seccomp_installer_tool=$harness_root/seccomp_installer.py
starting_state_tool=$harness_root/starting_state.py
manifest_overlay=$harness_root/manifest_overlay.py
lifecycle_evidence=$harness_root/lifecycle_evidence.py
state_root=${OPENFOLD2_EVIDENCE_ROOT:?set OPENFOLD2_EVIDENCE_ROOT to an absolute evidence directory}
run_dir=$state_root/runs/$run_id

kubeconfig=${KUBECONFIG:?set KUBECONFIG to an absolute kubeconfig path}
namespace=${BENCHMARK_NAMESPACE:-nim-fast-start}
cluster_id=${OPENFOLD2_CLUSTER_ID:?set OPENFOLD2_CLUSTER_ID}
project_id=${OPENFOLD2_PROJECT_ID:?set OPENFOLD2_PROJECT_ID}
node_group_id=${OPENFOLD2_NODE_GROUP_ID:?set OPENFOLD2_NODE_GROUP_ID}
allowed_server=${EXPECTED_API_SERVER:?set EXPECTED_API_SERVER to the exact Kubernetes API URL}
allowed_context=${OPENFOLD2_KUBE_CONTEXT:?set OPENFOLD2_KUBE_CONTEXT}
profile=${NEBIUS_PROFILE:?set NEBIUS_PROFILE}

artifact_pvc=mlspec-archvteams-2407-ckpt-m3
cache_pvc=openfold2-nim-cache
artifact_pv=${OPENFOLD2_ARTIFACT_PV:?set OPENFOLD2_ARTIFACT_PV}
cache_pv=${OPENFOLD2_CACHE_PV:?set OPENFOLD2_CACHE_PV}
holder_name=of2-artifact-holder-t12
holder_node=${OPENFOLD2_HOLDER_NODE:?set OPENFOLD2_HOLDER_NODE}
holder_manifest=$harness_root/manifests/artifact-holder.yaml
holder_manifest_sha=e9ef074cdcfa76fc25a561c950ff9eec7c359f981ff413ebd3b2b9f5260a5bad

target_name=of2-target-$run_id
target_pull_sa=of2-target-pull-$run_id
seccomp_installer_name=of2-seccomp-$run_id
worker_name=of2-restore-$run_id
probe_name=of2-semantic-$run_id
canary_name=of2-canary-$run_id
qualified_name=of2-qualified-$run_id
target_network_policy=of2-target-$run_id
probe_network_policy=of2-probe-$run_id

kubectl_cmd=(kubectl --kubeconfig "$kubeconfig" --context "$allowed_context")
nebius_cmd=("${NEBIUS_CLI:-nebius}")

utc_now() {
  date -u +%Y-%m-%dT%H:%M:%S.%NZ
}

write_timestamp() {
  utc_now > "$1"
}

capture_group() {
  "${nebius_cmd[@]}" mk8s node-group get \
    --id "$node_group_id" --profile "$profile" --format json --no-check-update
}

update_group_async() {
  local desired=$1
  local output=$2
  local demand_output=${3:-}
  local current=${output%.json}-input.json
  capture_group > "$current" || return 1
  jq -e --arg group "$node_group_id" --arg cluster "$cluster_id" \
    '.metadata.id==$group and .metadata.parent_id==$cluster and (.metadata.resource_version|type)=="string"' \
    "$current" >/dev/null || return 1
  local resource_version
  resource_version=$(jq -er '.metadata.resource_version' "$current") || return 1
  if [[ -n $demand_output ]]; then
    demand_at=$(utc_now)
    printf '%s\n' "$demand_at" > "$demand_output" || return 1
  fi
  "${nebius_cmd[@]}" mk8s node-group update \
    --id "$node_group_id" \
    --fixed-node-count "$desired" \
    --resource-version "$resource_version" \
    --async \
    --profile "$profile" \
    --format json \
    --no-check-update > "$output" || return 1
}

require_sha256() {
  local path=$1
  local expected=$2
  local label=$3
  local actual
  actual=$(sha256sum "$path" | awk '{print $1}')
  if [[ $actual != "$expected" ]]; then
    echo "refusing: $label SHA-256 changed ($actual)" >&2
    return 1
  fi
}

wait_group_counts() {
  local desired=$1
  local require_ready=$2
  local output=$3
  local deadline=$((SECONDS + 2400))
  while (( SECONDS < deadline )); do
    capture_group > "$output.tmp"
    mv "$output.tmp" "$output"
    if jq -e --argjson desired "$desired" --argjson ready "$require_ready" '
      (.spec.fixed_node_count|tonumber)==$desired and
      ((.status.target_node_count // "0")|tonumber)==$desired and
      ((.status.node_count // "0")|tonumber)==$desired and
      (((.status.ready_node_count // "0")|tonumber) >= $ready)
    ' "$output" >/dev/null; then
      return 0
    fi
    sleep 5
  done
  return 124
}

capture_relevant_attachments() {
  local output=$1
  "${kubectl_cmd[@]}" get volumeattachments.storage.k8s.io -o json |
    jq --arg artifact "$artifact_pv" --arg cache "$cache_pv" '
      {apiVersion,kind,metadata,items:[.items[] | select(.spec.source.persistentVolumeName==$artifact or .spec.source.persistentVolumeName==$cache)]}
    ' > "$output"
}

wait_attachments_absent() {
  local output=$1
  local deadline=$((SECONDS + 900))
  : > "$output.poll.ndjson"
  while (( SECONDS < deadline )); do
    capture_relevant_attachments "$output.tmp"
    jq -c --arg at "$(utc_now)" '{observed_at:$at,count:(.items|length),attachments:[.items[]|{name:.metadata.name,pv:.spec.source.persistentVolumeName,node:.spec.nodeName,attached:(.status.attached//false)}]}' \
      "$output.tmp" >> "$output.poll.ndjson"
    if jq -e '.items|length==0' "$output.tmp" >/dev/null; then
      mv "$output.tmp" "$output"
      return 0
    fi
    sleep 2
  done
  mv "$output.tmp" "$output"
  return 124
}

wait_attachments_on_node() {
  local node=$1
  local output=$2
  local deadline=$((SECONDS + 900))
  : > "$output.poll.ndjson"
  while (( SECONDS < deadline )); do
    capture_relevant_attachments "$output.tmp"
    jq -c --arg at "$(utc_now)" '{observed_at:$at,count:(.items|length),attachments:[.items[]|{name:.metadata.name,pv:.spec.source.persistentVolumeName,node:.spec.nodeName,attached:(.status.attached//false)}]}' \
      "$output.tmp" >> "$output.poll.ndjson"
    if jq -e --arg node "$node" --arg artifact "$artifact_pv" --arg cache "$cache_pv" '
      (.items|length)==2 and
      ([.items[].spec.source.persistentVolumeName]|sort)==([$artifact,$cache]|sort) and
      all(.items[]; .spec.nodeName==$node and .status.attached==true)
    ' "$output.tmp" >/dev/null; then
      mv "$output.tmp" "$output"
      return 0
    fi
    sleep 2
  done
  mv "$output.tmp" "$output"
  return 124
}

wait_node_count() {
  local desired=$1
  local output=$2
  local deadline=$((SECONDS + 1200))
  while (( SECONDS < deadline )); do
    "${kubectl_cmd[@]}" get nodes -l "nebius.com/node-group-id=$node_group_id" -o json > "$output.tmp"
    mv "$output.tmp" "$output"
    if [[ $(jq -r '.items|length' "$output") == "$desired" ]]; then
      if (( desired == 0 )) || jq -e 'all(.items[]; any(.status.conditions[]; .type=="Ready" and .status=="True"))' "$output" >/dev/null; then
        return 0
      fi
    fi
    sleep 5
  done
  return 124
}

require_only_node_services() {
  local node=$1
  local node_uid=$2
  local pods_json=$3
  jq -e --arg node "$node" --arg node_uid "$node_uid" \
    -f "$node_service_gate" "$pods_json" >/dev/null
}

wait_new_node_startup_taints() {
  local name=$1
  local uid=$2
  local output=$3
  local deadline=$((SECONDS + 300))
  local pending=$run_dir/new-node-startup-current.json
  local state=$run_dir/new-node-startup-state.tmp.json
  local poll=$run_dir/new-node-startup-poll.ndjson
  local errors=$run_dir/new-node-startup-errors.log
  : > "$poll"
  : > "$errors"
  while (( SECONDS < deadline )); do
    if ! "${kubectl_cmd[@]}" get node "$name" -o json > "$pending"; then
      return 1
    fi
    if ! jq -e \
      --arg name "$name" \
      --arg uid "$uid" \
      --arg group "$node_group_id" \
      '.apiVersion=="v1" and .kind=="Node" and
       .metadata.name==$name and .metadata.uid==$uid and .metadata.deletionTimestamp==null and
       .metadata.labels["nebius.com/node-group-id"]==$group and
       .metadata.labels["nebius.com/preemptible"]=="true" and
       .metadata.labels["nebius.com/gpu-name"]=="H100" and
       .metadata.labels["nebius.com/resource-preset"]=="1gpu-16vcpu-200gb" and
       .metadata.labels["nebius.com/nvidia_driver_version"]=="580.159.04-1ubuntu1" and
       .metadata.labels["nebius.com/cuda_version"]=="13.0.3-1" and
       .spec.providerID==("nebius://"+$name) and (.spec.unschedulable // false)==false and
       ([.status.conditions[] | select(.type=="Ready")]|length)==1 and
       any(.status.conditions[]; .type=="Ready" and .status=="True") and
       .status.nodeInfo.architecture=="amd64" and
       .status.nodeInfo.containerRuntimeVersion=="containerd://1.7.34" and
       .status.nodeInfo.kernelVersion=="6.11.0-1016-nvidia" and
       .status.nodeInfo.osImage=="Ubuntu 24.04.4 LTS"' \
      "$pending" >/dev/null; then
      echo "new node identity, readiness, or runtime drifted during startup-taint polling" >&2
      return 1
    fi
    if [[ ! -e $run_dir/new-node-startup-initial.json ]]; then
      cp "$pending" "$run_dir/new-node-startup-initial.json"
    fi
    if ! python3 "$node_admission" startup-state --node-json "$pending" \
      > "$state" 2>> "$errors"; then
      cp "$pending" "$run_dir/new-node-startup-rejected.json"
      return 1
    fi
    jq -c --arg at "$(utc_now)" --slurpfile state "$state" \
      '{observed_at:$at,node:{name:.metadata.name,uid:.metadata.uid,ready_transition_at:([.status.conditions[]|select(.type=="Ready")][0].lastTransitionTime)},status:$state[0].status,wait_reasons:$state[0].wait_reasons,taints:$state[0].taints,gpu_allocatable:$state[0].gpu_allocatable}' \
      "$pending" >> "$poll"
    if jq -e '.status=="clear"' "$state" >/dev/null; then
      mv "$pending" "$output"
      mv "$state" "$run_dir/new-node-startup-cleared.json"
      return 0
    fi
    sleep 2
  done
  cp "$pending" "$run_dir/new-node-startup-timeout.json"
  echo "timed out waiting for reviewed transient new-node startup taints to clear" >&2
  return 124
}

wait_criu_agent() {
  local node=$1
  local output=$2
  local deadline=$((SECONDS + 900))
  while (( SECONDS < deadline )); do
    "${kubectl_cmd[@]}" -n "$namespace" get pods --field-selector "spec.nodeName=$node" -o json > "$output.tmp"
    mv "$output.tmp" "$output"
    if jq -e '
      [.items[] | select(any(.metadata.ownerReferences[]?; .controller==true and .kind=="DaemonSet" and .name=="nim-criu-agent"))] as $agents |
      ($agents|length)==1 and all($agents[]; .status.phase=="Running" and all(.status.containerStatuses[]; .ready==true))
    ' "$output" >/dev/null; then
      return 0
    fi
    sleep 2
  done
  return 124
}

wait_for_job() {
  local name=$1
  local output=$2
  local deadline=$((SECONDS + 1200))
  while (( SECONDS < deadline )); do
    "${kubectl_cmd[@]}" -n "$namespace" get job "$name" -o json > "$output.tmp"
    mv "$output.tmp" "$output"
    if jq -e '(.status.succeeded // 0)==1 and (.status.failed // 0)==0' "$output" >/dev/null; then
      return 0
    fi
    if jq -e '(.status.failed // 0)>0' "$output" >/dev/null; then
      return 1
    fi
    sleep 1
  done
  return 124
}

raw_delete_with_uid() {
  local resource=$1
  local name=$2
  local uid=$3
  local evidence_stem=$4
  local uri
  case "$resource" in
    pod) uri="/api/v1/namespaces/$namespace/pods/$name" ;;
    configmap) uri="/api/v1/namespaces/$namespace/configmaps/$name" ;;
    service) uri="/api/v1/namespaces/$namespace/services/$name" ;;
    serviceaccount) uri="/api/v1/namespaces/$namespace/serviceaccounts/$name" ;;
    job) uri="/apis/batch/v1/namespaces/$namespace/jobs/$name" ;;
    role.rbac.authorization.k8s.io) uri="/apis/rbac.authorization.k8s.io/v1/namespaces/$namespace/roles/$name" ;;
    rolebinding.rbac.authorization.k8s.io) uri="/apis/rbac.authorization.k8s.io/v1/namespaces/$namespace/rolebindings/$name" ;;
    networkpolicy.networking.k8s.io) uri="/apis/networking.k8s.io/v1/namespaces/$namespace/networkpolicies/$name" ;;
    *)
      echo "refusing raw deletion for unsupported resource: $resource" >&2
      return 1
      ;;
  esac
  local options=$run_dir/$evidence_stem-delete-options.json
  local response=$run_dir/$evidence_stem-delete-response.json
  jq -n --arg uid "$uid" \
    '{apiVersion:"v1",kind:"DeleteOptions",propagationPolicy:"Foreground",preconditions:{uid:$uid}}' \
    > "$options" || return 1
  "${kubectl_cmd[@]}" delete --raw "$uri" -f "$options" > "$response" || return 1
  printf '%s\t%s\t%s\t%s\n' "$(utc_now)" "$resource" "$name" "$uid" \
    >> "$run_dir/cleanup-deleted.tsv" || return 1

  local deadline=$((SECONDS + 180))
  local observed=$run_dir/$evidence_stem-delete-wait.json
  while (( SECONDS < deadline )); do
    if ! "${kubectl_cmd[@]}" -n "$namespace" get "$resource" "$name" --ignore-not-found -o json > "$observed"; then
      return 1
    fi
    if [[ ! -s $observed ]]; then
      return 0
    fi
    if [[ $(jq -er '.metadata.uid' "$observed") != "$uid" ]]; then
      echo "delete wait refused $resource/$name: UID changed" >&2
      return 1
    fi
    sleep 1
  done
  echo "timed out deleting exact UID $uid for $resource/$name" >&2
  return 124
}

create_run_resource() {
  local manifest=$1
  local document_index=$2
  local resource=$3
  local name=$4
  local evidence_stem=$5
  local document=$run_dir/$evidence_stem-create.yaml
  local response=$run_dir/$evidence_stem-create-response.json
  yq eval "select(documentIndex == $document_index)" "$manifest" > "$document" || return 1
  if [[ ! -s $document ]]; then
    echo "manifest document $document_index is absent in $manifest" >&2
    return 1
  fi
  "${kubectl_cmd[@]}" -n "$namespace" create -f "$document" -o json > "$response" || return 1
  local uid
  uid=$(jq -er --arg name "$name" --arg namespace "$namespace" --arg run "$run_id" '
    select(
      .metadata.name==$name and .metadata.namespace==$namespace and
      .metadata.labels["archvteams.nebius.ai/run-id"]==$run and
      (.metadata.uid|type)=="string" and (.metadata.uid|length)>0
    ) | .metadata.uid
  ' "$response") || return 1
  printf '%s\t%s\t%s\n' "$resource" "$name" "$uid" >> "$run_dir/created-resource-uids.tsv" || return 1
}

owned_uid_for() {
  local resource=$1
  local name=$2
  local matches
  matches=$(awk -F '\t' -v resource="$resource" -v name="$name" \
    '$1==resource && $2==name {print $3}' "$run_dir/created-resource-uids.tsv") || return 1
  if [[ $(wc -l <<< "$matches") -ne 1 || -z $matches ]]; then
    echo "cleanup refused $resource/$name: no unique create-time UID" >&2
    return 1
  fi
  printf '%s\n' "$matches"
}

safe_delete_run_resource() {
  local resource=$1
  local name=$2
  local resource_stem=${resource//\//-}
  resource_stem=${resource_stem//./-}
  local current=$run_dir/cleanup-current-$resource_stem-$name.json
  if ! "${kubectl_cmd[@]}" -n "$namespace" get "$resource" "$name" --ignore-not-found -o json > "$current"; then
    return 1
  fi
  if [[ ! -s $current ]]; then
    return 0
  fi
  local uid label uid_check
  uid=$(owned_uid_for "$resource" "$name") || return 1
  label=$(jq -r '.metadata.labels["archvteams.nebius.ai/run-id"] // ""' "$current")
  if [[ $label != "$run_id" ]]; then
    echo "cleanup refused $resource/$name: run label mismatch" >&2
    return 1
  fi
  if ! uid_check=$("${kubectl_cmd[@]}" -n "$namespace" get "$resource" "$name" --ignore-not-found -o jsonpath='{.metadata.uid}'); then
    return 1
  fi
  if [[ -z $uid_check ]]; then
    return 0
  fi
  if [[ $uid_check != "$uid" ]]; then
    echo "cleanup refused $resource/$name: UID changed" >&2
    return 1
  fi
  raw_delete_with_uid "$resource" "$name" "$uid" "cleanup-$resource_stem-$name"
}

wait_run_resources_absent() {
  local deadline=$((SECONDS + 300))
  local output=$run_dir/resources-after-cleanup.json
  : > "$run_dir/resources-after-cleanup.poll.ndjson"
  while (( SECONDS < deadline )); do
    if ! "${kubectl_cmd[@]}" -n "$namespace" \
      get pod,job,service,serviceaccount,role,rolebinding,configmap,networkpolicy \
      -l "archvteams.nebius.ai/run-id=$run_id" -o json > "$output.tmp"; then
      return 1
    fi
    mv "$output.tmp" "$output" || return 1
    jq -c --arg at "$(utc_now)" \
      '{observed_at:$at,count:(.items|length),resources:[.items[]|{apiVersion,kind,name:.metadata.name,uid:.metadata.uid}]}' \
      "$output" >> "$run_dir/resources-after-cleanup.poll.ndjson" || return 1
    if jq -e '(.items|length)==0' "$output" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 124
}

require_run_resource_absent() {
  local resource=$1
  local name=$2
  local resource_stem=${resource//\//-}
  resource_stem=${resource_stem//./-}
  local output=$run_dir/preflight-absent-$resource_stem-$name.json
  if ! "${kubectl_cmd[@]}" -n "$namespace" get "$resource" "$name" --ignore-not-found -o json > "$output"; then
    return 1
  fi
  if [[ -s $output ]]; then
    echo "refusing: pre-existing resource name $resource/$name" >&2
    return 1
  fi
}

ensure_original_group_count() {
  local current=$run_dir/node-group-cleanup-current.json
  capture_group > "$current" || return 1
  local desired
  desired=$(jq -er '.spec.fixed_node_count|tonumber' "$current") || return 1
  if (( desired != original_desired )); then
    update_group_async "$original_desired" "$run_dir/node-group-restore-operation.json" || return 1
  fi
  wait_group_counts "$original_desired" "$original_desired" "$run_dir/node-group-restored.json" || return 1
}

restore_holder() {
  local current=$run_dir/holder-cleanup-current.json
  if ! "${kubectl_cmd[@]}" -n "$namespace" get pod "$holder_name" --ignore-not-found -o json > "$current"; then
    return 1
  fi
  if [[ -s $current ]] && jq -e '.metadata.deletionTimestamp != null' "$current" >/dev/null; then
    local deadline=$((SECONDS + 300))
    while (( SECONDS < deadline )); do
      if ! "${kubectl_cmd[@]}" -n "$namespace" get pod "$holder_name" --ignore-not-found -o json > "$current"; then
        return 1
      fi
      [[ -s $current ]] || break
      sleep 1
    done
    if [[ -s $current ]]; then
      echo "terminating holder did not disappear" >&2
      return 124
    fi
  fi
  local restored_uid
  if [[ -s $current ]]; then
    restored_uid=$(jq -er '.metadata.uid' "$current") || return 1
  else
    "${kubectl_cmd[@]}" create -f "$holder_manifest" -o json > "$run_dir/holder-recreate.json" || return 1
    restored_uid=$(jq -er --arg name "$holder_name" --arg namespace "$namespace" '
      select(.metadata.name==$name and .metadata.namespace==$namespace) | .metadata.uid
    ' "$run_dir/holder-recreate.json") || return 1
  fi
  "${kubectl_cmd[@]}" -n "$namespace" wait --for=condition=Ready "pod/$holder_name" --timeout=900s || return 1
  "${kubectl_cmd[@]}" -n "$namespace" get pod "$holder_name" -o json > "$run_dir/holder-restored.json" || return 1
  jq -e --arg uid "$restored_uid" --arg node "$holder_node" --arg artifact "$artifact_pvc" --arg cache "$cache_pvc" '
    .metadata.uid==$uid and .metadata.deletionTimestamp==null and .metadata.ownerReferences==null and
    .spec.nodeName==$node and .status.phase=="Running" and
    (.status.containerStatuses|length)>0 and all(.status.containerStatuses[]; .ready==true) and
    any(.spec.volumes[]; .persistentVolumeClaim.claimName==$artifact and .persistentVolumeClaim.readOnly==true) and
    any(.spec.volumes[]; .persistentVolumeClaim.claimName==$cache and .persistentVolumeClaim.readOnly==true)
  ' "$run_dir/holder-restored.json" >/dev/null || return 1
  wait_attachments_on_node "$holder_node" "$run_dir/volumeattachments-holder-restored.json" || return 1
  sleep 2
  "${kubectl_cmd[@]}" -n "$namespace" get pod "$holder_name" -o json > "$run_dir/holder-restored-confirmed.json" || return 1
  jq -e --arg uid "$restored_uid" '
    .metadata.uid==$uid and .metadata.deletionTimestamp==null and .status.phase=="Running" and
    (.status.containerStatuses|length)>0 and all(.status.containerStatuses[]; .ready==true)
  ' "$run_dir/holder-restored-confirmed.json" >/dev/null || return 1
  wait_attachments_on_node "$holder_node" "$run_dir/volumeattachments-holder-restored-confirmed.json" || return 1
}

cleanup() {
  local main_status=$?
  trap - EXIT INT TERM
  set +e
  local cleanup_failed=0
  write_timestamp "$run_dir/cleanup-started-at.txt"
  if (( cluster_verified == 1 )); then
    "${kubectl_cmd[@]}" -n "$namespace" get pod,job,service,serviceaccount,role,rolebinding,configmap,networkpolicy \
      -l "archvteams.nebius.ai/run-id=$run_id" -o json > "$run_dir/resources-before-cleanup.json" 2> "$run_dir/resources-before-cleanup.err"
    "${kubectl_cmd[@]}" -n "$namespace" get events --sort-by=.metadata.creationTimestamp -o json \
      > "$run_dir/events-before-cleanup.json" 2> "$run_dir/events-before-cleanup.err"

    safe_delete_run_resource job "$probe_name" || cleanup_failed=1
    safe_delete_run_resource job "$worker_name" || cleanup_failed=1
    safe_delete_run_resource pod "$target_name" || cleanup_failed=1
    safe_delete_run_resource pod "$seccomp_installer_name" || cleanup_failed=1
    safe_delete_run_resource configmap "$probe_name" || cleanup_failed=1
    safe_delete_run_resource rolebinding.rbac.authorization.k8s.io "$worker_name" || cleanup_failed=1
    safe_delete_run_resource role.rbac.authorization.k8s.io "$worker_name" || cleanup_failed=1
    safe_delete_run_resource serviceaccount "$worker_name" || cleanup_failed=1
    safe_delete_run_resource service "$canary_name" || cleanup_failed=1
    safe_delete_run_resource service "$qualified_name" || cleanup_failed=1
    safe_delete_run_resource networkpolicy.networking.k8s.io "$target_network_policy" || cleanup_failed=1
    safe_delete_run_resource networkpolicy.networking.k8s.io "$probe_network_policy" || cleanup_failed=1
    safe_delete_run_resource serviceaccount "$target_pull_sa" || cleanup_failed=1
    wait_run_resources_absent || cleanup_failed=1

    if (( holder_released == 1 )); then
      wait_attachments_absent "$run_dir/volumeattachments-after-run-cleanup.json" || cleanup_failed=1
      write_timestamp "$run_dir/run-volumes-detached-at.txt"
    fi
  fi
  if (( group_verified == 1 )); then
    ensure_original_group_count || cleanup_failed=1
  fi
  if (( cluster_verified == 1 && holder_released == 1 )); then
    restore_holder || cleanup_failed=1
  fi
  if (( group_verified == 1 )); then
    capture_group > "$run_dir/node-group-final.json" || cleanup_failed=1
    "${kubectl_cmd[@]}" get nodes -l "nebius.com/node-group-id=$node_group_id" -o json > "$run_dir/nodes-final.json" || cleanup_failed=1
  fi
  write_timestamp "$run_dir/cleanup-finished-at.txt"
  if (( group_verified == 1 )); then
    python3 "$lifecycle_evidence" \
      --run-dir "$run_dir" \
      --main-status "$main_status" \
      --cleanup-failed "$cleanup_failed" \
      --holder-released "$holder_released" \
      --output "$run_dir/lifecycle-evidence.json" \
      > "$run_dir/lifecycle-evidence.out" 2> "$run_dir/lifecycle-evidence.err" || cleanup_failed=1
  fi
  jq -n \
    --argjson main_status "$main_status" \
    --argjson cleanup_failed "$cleanup_failed" \
    --argjson original_desired "$original_desired" \
    --arg run_id "$run_id" \
    --arg finished_at "$(utc_now)" \
    '{schema:"archvteams.nebius.ai/openfold2-newnode-cleanup/v1",run_id:$run_id,main_exit_status:$main_status,cleanup_failed:($cleanup_failed==1),original_desired_count:$original_desired,finished_at:$finished_at}' \
    > "$run_dir/cleanup-status.json"
  if (( cleanup_failed != 0 )); then
    exit 90
  fi
  exit "$main_status"
}

install -d -m 700 "$state_root/runs"
exec {benchmark_lock_fd}> "$state_root/benchmark.lock"
if ! flock -n "$benchmark_lock_fd"; then
  echo "another OpenFold2 new-node benchmark owns the local execution lock" >&2
  exit 75
fi
if [[ -e $run_dir ]]; then
  echo "run directory already exists: $run_dir" >&2
  exit 73
fi
install -d -m 700 "$run_dir"
: > "$run_dir/cleanup-deleted.tsv"
: > "$run_dir/created-resource-uids.tsv"
original_desired=1
cluster_verified=0
group_verified=0
holder_released=0
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

write_timestamp "$run_dir/preflight-started-at.txt"

server=$("${kubectl_cmd[@]}" config view --minify -o jsonpath='{.clusters[0].cluster.server}')
if [[ $server != "$allowed_server" ]]; then
  echo "kubeconfig is not bound to the one allowed cluster" >&2
  exit 78
fi
"${nebius_cmd[@]}" mk8s cluster get --id "$cluster_id" --profile "$profile" --format json --no-check-update \
  > "$run_dir/cluster-preflight.json"
jq -e --arg cluster "$cluster_id" --arg project "$project_id" \
  '.metadata.id==$cluster and .metadata.parent_id==$project and .status.state=="RUNNING"' \
  "$run_dir/cluster-preflight.json" >/dev/null
cluster_verified=1
capture_group > "$run_dir/node-group-original.json"
"${kubectl_cmd[@]}" get nodes -l "nebius.com/node-group-id=$node_group_id" -o json > "$run_dir/previous-nodes.json"
python3 "$starting_state_tool" \
  --node-group-json "$run_dir/node-group-original.json" \
  --nodes-json "$run_dir/previous-nodes.json" \
  --collected-at "$(utc_now)" \
  --output "$run_dir/starting-state.json" > "$run_dir/starting-state.out"
starting_mode=$(jq -er '.mode' "$run_dir/starting-state.json")
original_desired=$(jq -er '.node_group.fixed' "$run_dir/starting-state.json")
jq '.items[0]' "$run_dir/previous-nodes.json" > "$run_dir/previous-node-initial.json"
previous_node=$(jq -er '.metadata.name' "$run_dir/previous-node-initial.json")
previous_uid=$(jq -er '.metadata.uid' "$run_dir/previous-node-initial.json")
"${kubectl_cmd[@]}" get pods -A --field-selector "spec.nodeName=$previous_node" -o json > "$run_dir/previous-node-pods.json"
if [[ $starting_mode == healthy ]]; then
  require_only_node_services "$previous_node" "$previous_uid" "$run_dir/previous-node-pods.json"
else
  jq -e --arg artifact "$artifact_pvc" --arg cache "$cache_pvc" '
    all(.items[]; all(.spec.volumes[]?; .persistentVolumeClaim.claimName!=$artifact and .persistentVolumeClaim.claimName!=$cache))
  ' "$run_dir/previous-node-pods.json" >/dev/null
fi
"${kubectl_cmd[@]}" -n "$namespace" \
  get pod,job,service,serviceaccount,role,rolebinding,configmap,networkpolicy \
  -l "archvteams.nebius.ai/run-id=$run_id" -o json > "$run_dir/resources-preexisting.json"
jq -e '(.items|length)==0' "$run_dir/resources-preexisting.json" >/dev/null
require_run_resource_absent pod "$target_name"
require_run_resource_absent pod "$seccomp_installer_name"
require_run_resource_absent serviceaccount "$target_pull_sa"
require_run_resource_absent service "$canary_name"
require_run_resource_absent service "$qualified_name"
require_run_resource_absent networkpolicy.networking.k8s.io "$target_network_policy"
require_run_resource_absent networkpolicy.networking.k8s.io "$probe_network_policy"
require_run_resource_absent serviceaccount "$worker_name"
require_run_resource_absent role.rbac.authorization.k8s.io "$worker_name"
require_run_resource_absent rolebinding.rbac.authorization.k8s.io "$worker_name"
require_run_resource_absent job "$worker_name"
require_run_resource_absent configmap "$probe_name"
require_run_resource_absent job "$probe_name"

secret=archvteams-2407-registry-pull
"${kubectl_cmd[@]}" -n "$namespace" get secret "$secret" -o json |
  jq '{apiVersion,kind,metadata:{name:.metadata.name,namespace:.metadata.namespace,uid:.metadata.uid,resourceVersion:.metadata.resourceVersion},type}' \
  > "$run_dir/secret-ref-$secret.json"
jq -e '.type=="kubernetes.io/dockerconfigjson"' "$run_dir/secret-ref-$secret.json" >/dev/null

require_sha256 "$seccomp_installer_tool" "69ecec504eb049720f606e8de88d0bb9600a6bce11b37f38532dc6dee2d8c3b8" "new-node seccomp installer"
"${kubectl_cmd[@]}" -n "$namespace" get configmap archvteams-2407-native-snapshot-seccomp -o json \
  > "$run_dir/seccomp-configmap.json"
python3 "$seccomp_installer_tool" validate-configmap \
  --configmap-json "$run_dir/seccomp-configmap.json" \
  > "$run_dir/seccomp-configmap-receipt.json"

if [[ $(sha256sum "$holder_manifest" | awk '{print $1}') != "$holder_manifest_sha" ]]; then
  echo "trusted holder manifest digest changed" >&2
  exit 78
fi
"${kubectl_cmd[@]}" -n "$namespace" get pod "$holder_name" -o json > "$run_dir/holder-before.json"
jq -e --arg node "$holder_node" --arg artifact "$artifact_pvc" --arg cache "$cache_pvc" '
  .metadata.deletionTimestamp==null and .metadata.ownerReferences==null and
  .spec.nodeName==$node and .status.phase=="Running" and
  (.status.containerStatuses|length)>0 and all(.status.containerStatuses[]; .ready==true) and
  any(.spec.volumes[]; .persistentVolumeClaim.claimName==$artifact and .persistentVolumeClaim.readOnly==true) and
  any(.spec.volumes[]; .persistentVolumeClaim.claimName==$cache and .persistentVolumeClaim.readOnly==true)
' "$run_dir/holder-before.json" >/dev/null
holder_uid=$(jq -er '.metadata.uid' "$run_dir/holder-before.json")
"${kubectl_cmd[@]}" -n "$namespace" get pvc "$artifact_pvc" "$cache_pvc" -o json > "$run_dir/pvcs-before.json"
jq -e --arg artifact "$artifact_pv" --arg cache "$cache_pv" '
  (.items|length)==2 and ([.items[].spec.volumeName]|sort)==([$artifact,$cache]|sort) and all(.items[]; .status.phase=="Bound" and .spec.accessModes==["ReadWriteOnce"])
' "$run_dir/pvcs-before.json" >/dev/null
wait_attachments_on_node "$holder_node" "$run_dir/volumeattachments-before.json"

sha256sum \
  "$pipeline_root/render.py" \
  "$pipeline_root/lint_manifest.py" \
  "$pipeline_root/bind_target.py" \
  "$pipeline_root/evidence.py" \
  "$pipeline_root/manifests/target.yaml.tmpl" \
  "$pipeline_root/manifests/restore-worker.yaml.tmpl" \
  "$pipeline_root/manifests/semantic-probe.yaml.tmpl" \
  "$pipeline_root/restore-interface.live.json" \
  "$pipeline_root/../validate_openfold2.py" > "$run_dir/frozen-pipeline.sha256"
require_sha256 "$pipeline_root/render.py" "95ef0a86aee8022d8fa301c98097115ebeef2dc13a86813558811e61ae748ffc" "frozen renderer"
require_sha256 "$pipeline_root/lint_manifest.py" "79ad4c714933d434920f7e2092853f090707adda8d5c422044fbf212ab9285ae" "frozen manifest linter"
require_sha256 "$pipeline_root/bind_target.py" "88613cdc726fe01a3be9aed38a8b2624677aea5b73d69b410905bc57f985cc97" "frozen target binder"
require_sha256 "$pipeline_root/evidence.py" "2372b5ace1ac4e515dc45f4b45443bb486087e8c375de949ee35f33d3c313b2b" "frozen evidence validator"
require_sha256 "$pipeline_root/manifests/target.yaml.tmpl" "04d68f1d7d7eb723443bada5e651986c0b21fb4ce343eb0793a8fdf3b07f9826" "frozen target template"
require_sha256 "$pipeline_root/manifests/restore-worker.yaml.tmpl" "717e22f17a2eb5d2792d3e55753ecf0ade8e46a3ce956964dbb24898f89cfe41" "frozen restore template"
require_sha256 "$pipeline_root/manifests/semantic-probe.yaml.tmpl" "918a412f90c1923fac03c13b6ad2fabd84db589e32277c0bb0e854312f7e503e" "frozen probe template"
require_sha256 "$pipeline_root/restore-interface.live.json" "3dd37721a990176d5ed37fe0c4435c2f6057e81db06b51a1b93f5053618d8a3f" "frozen live contract"
require_sha256 "$pipeline_root/../validate_openfold2.py" "4f3e70ef29ea9cd3113c09e6f63bd15b4d9826bf64d7d16972c6c3d0eef3090e" "frozen OpenFold2 validator"
require_sha256 "$starting_state_tool" "50a7f65b792c951685cd47f375d837e9293c9e14ddcd900e409c2ce7eb6bb787" "retiring-node starting-state gate"
require_sha256 "$node_admission" "e81f19b87e083fe21f14e81d851b3facf7976f90c9101a61f84cad24cd79eff4" "bounded startup/admission gate"
require_sha256 "$node_service_gate" "470a703e91901fafe2bbb6ef11cc73c80efc94f30d8782fb13bb8624249c3dbb" "node service-occupancy gate"
require_sha256 "$seccomp_installer_tool" "69ecec504eb049720f606e8de88d0bb9600a6bce11b37f38532dc6dee2d8c3b8" "new-node seccomp installer"
sha256sum "$starting_state_tool" "$node_admission" "$node_service_gate" "$seccomp_installer_tool" \
  > "$run_dir/harness-admission-tools.sha256"
jq -e '
  .approved==true and
  .source.commit=="f7f37be174d252590c4b56e25ff4262dd82466fd" and
  .source.patch_sha256=="260c1d9a7f192b8c0b25c924ab26b43a95ad599d38d3f367383e3e984aecfd11" and
  .worker_image=="registry.example.invalid/faststart/snapshot-agent@sha256:31e1dacd18b99aec1ab7e8ec8c933f260c9dcec687938b40c44c61274f930d86" and
  .validator_sha256=="4f3e70ef29ea9cd3113c09e6f63bd15b4d9826bf64d7d16972c6c3d0eef3090e"
' "$pipeline_root/restore-interface.live.json" >/dev/null

zero_established=0
if [[ $starting_mode == retiring-unknown ]]; then
  capture_group > "$run_dir/node-group-pre-recovery-zero.json"
  "${kubectl_cmd[@]}" get nodes -l "nebius.com/node-group-id=$node_group_id" -o json \
    > "$run_dir/nodes-pre-recovery-zero.json"
  python3 "$starting_state_tool" \
    --node-group-json "$run_dir/node-group-pre-recovery-zero.json" \
    --nodes-json "$run_dir/nodes-pre-recovery-zero.json" \
    --collected-at "$(utc_now)" \
    --output "$run_dir/starting-state-pre-recovery-zero.json" \
    > "$run_dir/starting-state-pre-recovery-zero.out"
  jq -e --arg name "$previous_node" --arg uid "$previous_uid" '
    .mode=="retiring-unknown" and .node.name==$name and .node.uid==$uid
  ' "$run_dir/starting-state-pre-recovery-zero.json" >/dev/null
  jq '.items[0]' "$run_dir/nodes-pre-recovery-zero.json" > "$run_dir/previous-node.json"
  write_timestamp "$run_dir/scale-zero-requested-at.txt"
  group_verified=1
  update_group_async 0 "$run_dir/scale-zero-operation.json"
  wait_group_counts 0 0 "$run_dir/node-group-zero.json"
  wait_node_count 0 "$run_dir/nodes-zero.json"
  "${kubectl_cmd[@]}" get node "$previous_node" --ignore-not-found -o json \
    > "$run_dir/retiring-node-after-zero.json"
  if [[ -s $run_dir/retiring-node-after-zero.json ]]; then
    echo "retiring predecessor still exists after group reached zero" >&2
    exit 78
  fi
  jq -n --arg name "$previous_node" --arg uid "$previous_uid" --arg at "$(utc_now)" \
    '{schema:"archvteams.nebius.ai/openfold2-retiring-predecessor/v1",name:$name,uid:$uid,absent:true,observed_at:$at}' \
    > "$run_dir/retiring-predecessor-removed.json"
  write_timestamp "$run_dir/zero-state-ready-at.txt"
  zero_established=1
fi

write_timestamp "$run_dir/holder-delete-requested-at.txt"
"${kubectl_cmd[@]}" -n "$namespace" get pod "$holder_name" -o json > "$run_dir/holder-before-delete.json"
jq -e --arg uid "$holder_uid" --arg node "$holder_node" --arg artifact "$artifact_pvc" --arg cache "$cache_pvc" '
  .metadata.uid==$uid and .metadata.deletionTimestamp==null and .metadata.ownerReferences==null and
  .spec.nodeName==$node and .status.phase=="Running" and
  (.status.containerStatuses|length)>0 and all(.status.containerStatuses[]; .ready==true) and
  any(.spec.volumes[]; .persistentVolumeClaim.claimName==$artifact and .persistentVolumeClaim.readOnly==true) and
  any(.spec.volumes[]; .persistentVolumeClaim.claimName==$cache and .persistentVolumeClaim.readOnly==true)
' "$run_dir/holder-before-delete.json" >/dev/null
wait_attachments_on_node "$holder_node" "$run_dir/volumeattachments-before-holder-delete.json"
holder_released=1
raw_delete_with_uid pod "$holder_name" "$holder_uid" holder
wait_attachments_absent "$run_dir/volumeattachments-prepared-detached.json"
write_timestamp "$run_dir/holder-volumes-detached-at.txt"

if (( zero_established == 0 )); then
  capture_group > "$run_dir/node-group-pre-scale-zero.json"
  "${kubectl_cmd[@]}" get nodes -l "nebius.com/node-group-id=$node_group_id" -o json \
    > "$run_dir/nodes-pre-scale-zero.json"
  python3 "$starting_state_tool" \
    --node-group-json "$run_dir/node-group-pre-scale-zero.json" \
    --nodes-json "$run_dir/nodes-pre-scale-zero.json" \
    --collected-at "$(utc_now)" \
    --output "$run_dir/starting-state-pre-scale-zero.json" > "$run_dir/starting-state-pre-scale-zero.out"
  jq -e '.mode=="healthy"' "$run_dir/starting-state-pre-scale-zero.json" >/dev/null
  jq '.items[0]' "$run_dir/nodes-pre-scale-zero.json" > "$run_dir/previous-node.json"
  previous_node=$(jq -er '.metadata.name' "$run_dir/previous-node.json")
  previous_uid=$(jq -er '.metadata.uid' "$run_dir/previous-node.json")
  "${kubectl_cmd[@]}" get pods -A --field-selector "spec.nodeName=$previous_node" -o json \
    > "$run_dir/previous-node-pods-pre-scale-zero.json"
  require_only_node_services "$previous_node" "$previous_uid" \
    "$run_dir/previous-node-pods-pre-scale-zero.json"
  write_timestamp "$run_dir/scale-zero-requested-at.txt"
  group_verified=1
  update_group_async 0 "$run_dir/scale-zero-operation.json"
  wait_group_counts 0 0 "$run_dir/node-group-zero.json"
  wait_node_count 0 "$run_dir/nodes-zero.json"
  write_timestamp "$run_dir/zero-state-ready-at.txt"
else
  wait_group_counts 0 0 "$run_dir/node-group-zero-after-holder-detach.json"
  wait_node_count 0 "$run_dir/nodes-zero-after-holder-detach.json"
fi

update_group_async 1 "$run_dir/scale-up-operation.json" "$run_dir/scale-up-demand-at.txt"
write_timestamp "$run_dir/scale-up-request-returned-at.txt"
wait_group_counts 1 1 "$run_dir/node-group-new-ready.json"
wait_node_count 1 "$run_dir/new-nodes.json"
new_node_candidate=$(jq -er '.items[0].metadata.name' "$run_dir/new-nodes.json")
new_node_candidate_uid=$(jq -er '.items[0].metadata.uid' "$run_dir/new-nodes.json")
if [[ $new_node_candidate == "$previous_node" || $new_node_candidate_uid == "$previous_uid" ]]; then
  echo "scaled node reused the predecessor name or UID" >&2
  exit 78
fi
wait_new_node_startup_taints "$new_node_candidate" "$new_node_candidate_uid" "$run_dir/new-node.json"
python3 "$node_admission" build \
  --node-json "$run_dir/new-node.json" \
  --previous-node-json "$run_dir/previous-node.json" \
  --collected-at "$(utc_now)" \
  --output "$run_dir/node-admission.json" > "$run_dir/node-admission.out"
new_node=$(jq -er '.node.name' "$run_dir/node-admission.json")
write_timestamp "$run_dir/new-node-admitted-at.txt"
wait_criu_agent "$new_node" "$run_dir/new-node-daemon-pods.json"
write_timestamp "$run_dir/criu-agent-ready-at.txt"

python3 "$seccomp_installer_tool" render \
  --run-id "$run_id" \
  --node "$new_node" \
  --output "$run_dir/seccomp-installer.yaml" \
  > "$run_dir/seccomp-installer-render.out"
write_timestamp "$run_dir/seccomp-installer-submit-at.txt"
create_run_resource "$run_dir/seccomp-installer.yaml" 0 pod "$seccomp_installer_name" seccomp-installer
"${kubectl_cmd[@]}" -n "$namespace" wait \
  --for=condition=Ready "pod/$seccomp_installer_name" --timeout=300s
"${kubectl_cmd[@]}" -n "$namespace" get pod "$seccomp_installer_name" -o json \
  > "$run_dir/seccomp-installer-ready.json"
seccomp_installer_uid=$(owned_uid_for pod "$seccomp_installer_name")
python3 "$seccomp_installer_tool" verify \
  --pod-json "$run_dir/seccomp-installer-ready.json" \
  --run-id "$run_id" \
  --node "$new_node" \
  --uid "$seccomp_installer_uid" \
  > "$run_dir/seccomp-installer-receipt.json"
"${kubectl_cmd[@]}" -n "$namespace" logs "$seccomp_installer_name" \
  > "$run_dir/seccomp-installer.log"
grep -Fx 'ebbe5e221b6b331bb84efbdfea7adb88e9dddab62a2ea901598bad09fe7f76a0  /host-seccomp/profiles/block-iouring.json' \
  "$run_dir/seccomp-installer.log" >/dev/null
write_timestamp "$run_dir/seccomp-installer-ready-at.txt"

cp "$pipeline_root/restore-interface.live.json" "$run_dir/restore-interface.json"
sha256sum "$run_dir/restore-interface.json" > "$run_dir/restore-interface.sha256"
jq -n \
  --arg run_id "$run_id" \
  --arg demand_at "$demand_at" \
  --arg node "$new_node" \
  --arg artifact_pvc "$artifact_pvc" \
  --arg cache_pvc "$cache_pvc" \
  '{
    schema:"archvteams.nebius.ai/openfold2-faststart-run/v1",
    demand_at:$demand_at,
    run_id:$run_id,
    target_node:$node,
    checkpoint_id:"openfold2-native-f7-v1",
    artifact_version:"1",
    artifact_manifest_sha256:"78368af3e6f143d7dc681632c4150b29f6354717103638b56e776244d9631b04",
    artifact_pvc:$artifact_pvc,
    cache_pvc:$cache_pvc
  }' > "$run_dir/run.json"

pipeline=(python3 "$runtime_pipeline" --pipeline-root "$pipeline_root" --admission "$run_dir/node-admission.json" --node-json "$run_dir/new-node.json")
"${pipeline[@]}" render target --contract "$run_dir/restore-interface.json" --run-config "$run_dir/run.json" \
  > "$run_dir/target.base.yaml"
python3 "$manifest_overlay" target --run-id "$run_id" --input "$run_dir/target.base.yaml" \
  --output "$run_dir/target.yaml" --service-account-output "$run_dir/target-pull-serviceaccount.yaml"
"${pipeline[@]}" lint "$run_dir/target.yaml" > "$run_dir/target.lint.out"

create_run_resource "$run_dir/target-pull-serviceaccount.yaml" 0 serviceaccount "$target_pull_sa" target-pull-serviceaccount
"${kubectl_cmd[@]}" -n "$namespace" get serviceaccount "$target_pull_sa" -o json \
  > "$run_dir/target-pull-serviceaccount-live.json"
jq -e --arg name "$target_pull_sa" --arg run "$run_id" '
  .metadata.name==$name and .metadata.labels["archvteams.nebius.ai/run-id"]==$run and
  .automountServiceAccountToken==false and .imagePullSecrets==[{"name":"archvteams-2407-registry-pull"}]
' "$run_dir/target-pull-serviceaccount-live.json" >/dev/null
create_run_resource "$run_dir/target.yaml" 1 service "$canary_name" canary-service
create_run_resource "$run_dir/target.yaml" 2 service "$qualified_name" qualified-service
create_run_resource "$run_dir/target.yaml" 3 networkpolicy.networking.k8s.io "$target_network_policy" target-network-policy
create_run_resource "$run_dir/target.yaml" 4 networkpolicy.networking.k8s.io "$probe_network_policy" probe-network-policy
write_timestamp "$run_dir/target-submit-at.txt"
create_run_resource "$run_dir/target.yaml" 0 pod "$target_name" target
"${kubectl_cmd[@]}" -n "$namespace" wait \
  --for=jsonpath='{.status.containerStatuses[0].state.running}' "pod/$target_name" --timeout=1800s
"${kubectl_cmd[@]}" -n "$namespace" get pod "$target_name" -o json > "$run_dir/target-before-binding.json"
jq -e --arg node "$new_node" '
  .spec.nodeName==$node and .spec.serviceAccountName==("of2-target-pull-"+.metadata.labels["archvteams.nebius.ai/run-id"]) and
  .spec.imagePullSecrets==[{"name":"archvteams-2407-registry-pull"}] and
  .status.phase=="Running" and .status.containerStatuses[0].state.running!=null
' "$run_dir/target-before-binding.json" >/dev/null
wait_attachments_on_node "$new_node" "$run_dir/volumeattachments-target-attached.json"
write_timestamp "$run_dir/target-placeholder-running-at.txt"
safe_delete_run_resource pod "$seccomp_installer_name"
write_timestamp "$run_dir/seccomp-installer-deleted-at.txt"

"${pipeline[@]}" bind \
  --contract "$run_dir/restore-interface.json" \
  --run-config "$run_dir/run.json" \
  --pod-json "$run_dir/target-before-binding.json" \
  --collected-at "$(utc_now)" \
  --binding-output "$run_dir/binding.json" \
  --patch-output "$run_dir/target-pod-spec.patch.json" > "$run_dir/bind.out"
"${kubectl_cmd[@]}" -n "$namespace" patch pod "$target_name" --type=json \
  --patch-file="$run_dir/target-pod-spec.patch.json" -o json > "$run_dir/target-patch-response.json"
"${kubectl_cmd[@]}" -n "$namespace" get pod "$target_name" -o json > "$run_dir/target-bound.json"
jq -e --arg hash "$(jq -er '.pod_spec_sha256' "$run_dir/binding.json")" \
  '.metadata.annotations["archvteams.nebius.ai/target-pod-spec-sha256"]==$hash' "$run_dir/target-bound.json" >/dev/null

"${pipeline[@]}" render restore --contract "$run_dir/restore-interface.json" \
  --run-config "$run_dir/run.json" --binding "$run_dir/binding.json" > "$run_dir/restore-worker.base.yaml"
python3 "$manifest_overlay" restore --run-id "$run_id" --input "$run_dir/restore-worker.base.yaml" \
  --output "$run_dir/restore-worker.yaml"
"${pipeline[@]}" lint "$run_dir/restore-worker.yaml" > "$run_dir/restore-worker.lint.out"
"${pipeline[@]}" render probe --contract "$run_dir/restore-interface.json" \
  --run-config "$run_dir/run.json" --binding "$run_dir/binding.json" > "$run_dir/semantic-probe.yaml"
"${pipeline[@]}" lint "$run_dir/semantic-probe.yaml" > "$run_dir/semantic-probe.lint.out"

create_run_resource "$run_dir/semantic-probe.yaml" 0 configmap "$probe_name" probe-configmap
write_timestamp "$run_dir/probe-submit-at.txt"
create_run_resource "$run_dir/semantic-probe.yaml" 1 job "$probe_name" probe-job
create_run_resource "$run_dir/restore-worker.yaml" 0 serviceaccount "$worker_name" worker-serviceaccount
create_run_resource "$run_dir/restore-worker.yaml" 1 role.rbac.authorization.k8s.io "$worker_name" worker-role
create_run_resource "$run_dir/restore-worker.yaml" 2 rolebinding.rbac.authorization.k8s.io "$worker_name" worker-rolebinding
write_timestamp "$run_dir/worker-submit-at.txt"
create_run_resource "$run_dir/restore-worker.yaml" 3 job "$worker_name" worker-job
"${kubectl_cmd[@]}" -n "$namespace" get serviceaccount "$worker_name" -o json \
  > "$run_dir/worker-serviceaccount-live.json"
jq -e --arg name "$worker_name" --arg run "$run_id" '
  .metadata.name==$name and .metadata.labels["archvteams.nebius.ai/run-id"]==$run and
  .imagePullSecrets==[{"name":"archvteams-2407-registry-pull"}]
' "$run_dir/worker-serviceaccount-live.json" >/dev/null

if ! wait_for_job "$worker_name" "$run_dir/worker-job.json"; then
  "${kubectl_cmd[@]}" -n "$namespace" get pods -l "job-name=$worker_name" -o json > "$run_dir/worker-pods.failed.json"
  worker_failed_pod=$(jq -r '.items[0].metadata.name // empty' "$run_dir/worker-pods.failed.json")
  if [[ -n $worker_failed_pod ]]; then
    "${kubectl_cmd[@]}" -n "$namespace" logs "$worker_failed_pod" > "$run_dir/worker.failed.log" 2>&1
  fi
  exit 1
fi
"${kubectl_cmd[@]}" -n "$namespace" wait --for=condition=Ready "pod/$target_name" --timeout=300s
if ! wait_for_job "$probe_name" "$run_dir/probe-job.json"; then
  "${kubectl_cmd[@]}" -n "$namespace" get pods -l "job-name=$probe_name" -o json > "$run_dir/probe-pods.failed.json"
  probe_failed_pod=$(jq -r '.items[0].metadata.name // empty' "$run_dir/probe-pods.failed.json")
  if [[ -n $probe_failed_pod ]]; then
    "${kubectl_cmd[@]}" -n "$namespace" logs "$probe_failed_pod" > "$run_dir/probe.failed.log" 2>&1
  fi
  exit 1
fi

"${kubectl_cmd[@]}" -n "$namespace" get pods -l "job-name=$worker_name" -o json > "$run_dir/worker-pods.json"
worker_pod=$(jq -er 'select((.items|length)==1)|.items[0].metadata.name' "$run_dir/worker-pods.json")
"${kubectl_cmd[@]}" -n "$namespace" get pod "$worker_pod" -o json > "$run_dir/worker-pod.json"
jq -e --arg node "$new_node" --arg account "$worker_name" '
  .spec.nodeName==$node and .spec.serviceAccountName==$account and
  .spec.imagePullSecrets==[{"name":"archvteams-2407-registry-pull"}]
' "$run_dir/worker-pod.json" >/dev/null
"${kubectl_cmd[@]}" -n "$namespace" logs "$worker_pod" > "$run_dir/worker.log"
tail -1 "$run_dir/worker.log" | jq -e -c 'select(.status=="succeeded")' > "$run_dir/worker-receipt.json"

"${kubectl_cmd[@]}" -n "$namespace" get pods -l "job-name=$probe_name" -o json > "$run_dir/probe-pods.json"
probe_pod=$(jq -er 'select((.items|length)==1)|.items[0].metadata.name' "$run_dir/probe-pods.json")
"${kubectl_cmd[@]}" -n "$namespace" get pod "$probe_pod" -o json > "$run_dir/probe-pod.json"
jq -e '(.spec.imagePullSecrets // [])==[]' "$run_dir/probe-pod.json" >/dev/null
"${kubectl_cmd[@]}" -n "$namespace" logs "$probe_pod" > "$run_dir/semantic-probe.log"
tail -1 "$run_dir/semantic-probe.log" | jq -e -c \
  'select(.status=="PASS" and .passed_case_count==2 and .failed_case_count==0)' > "$run_dir/semantic-summary.json"

"${kubectl_cmd[@]}" -n "$namespace" get pod "$target_name" -o json > "$run_dir/target-final.json"
"${kubectl_cmd[@]}" -n "$namespace" get service "$canary_name" -o json > "$run_dir/canary-service.json"
"${kubectl_cmd[@]}" -n "$namespace" get endpointslices.discovery.k8s.io \
  -l "kubernetes.io/service-name=$canary_name" -o json > "$run_dir/canary-endpointslices.json"
"${kubectl_cmd[@]}" get node "$new_node" -o json > "$run_dir/new-node-final.json"
capture_relevant_attachments "$run_dir/volumeattachments-before-evidence.json"

"${pipeline[@]}" evidence \
  --contract "$run_dir/restore-interface.json" \
  --run-config "$run_dir/run.json" \
  --binding "$run_dir/binding.json" \
  --target-pod "$run_dir/target-final.json" \
  --service "$run_dir/canary-service.json" \
  --endpoint-slices "$run_dir/canary-endpointslices.json" \
  --worker-job "$run_dir/worker-job.json" \
  --worker-pod "$run_dir/worker-pod.json" \
  --worker-receipt "$run_dir/worker-receipt.json" \
  --probe-job "$run_dir/probe-job.json" \
  --probe-pod "$run_dir/probe-pod.json" \
  --semantic-summary "$run_dir/semantic-summary.json" > "$run_dir/canary-evidence.json"
jq -e 'select(.status=="PASS" and .request_count==2 and .semantic_pass_count==2)' \
  "$run_dir/canary-evidence.json" >/dev/null
write_timestamp "$run_dir/benchmark-passed-at.txt"
jq '{run_id,status,timings_seconds,target}' "$run_dir/canary-evidence.json"
