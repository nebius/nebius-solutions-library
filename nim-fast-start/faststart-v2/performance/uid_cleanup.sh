#!/usr/bin/env bash

# UID-preconditioned cleanup shared by the OpenFold2 and Boltz2 trial runners.
# The caller invokes this from an EXIT trap with errexit disabled.  A missing,
# partial, foreign, or changing create receipt fails closed without name-only
# deletion.

UID_CLEANUP_PROXY_PID=""
UID_CLEANUP_PROXY_URL=""

uid_create_bundle() {
  local kubectl_array_name=$1 response_path=$2 create_log=$3
  shift 3
  local -n create_kubectl=$kubectl_array_name
  local response_partial="${response_path}.partial"
  local response_items="${response_path}.items.ndjson"
  local item_partial="${response_path}.item.partial"
  local manifest create_exit=0 bundle_failure=0
  (($# > 0)) || return 64
  : > "$response_items"
  for manifest in "$@"; do
    if [[ ! -f $manifest || -L $manifest ]]; then
      bundle_failure=1
      break
    fi
    "${create_kubectl[@]}" create -f "$manifest" -o json \
      > "$item_partial" 2>> "$create_log"
    create_exit=$?
    if ! jq -ce '
      select(type == "object")
      | select(.apiVersion | type == "string" and length > 0)
      | select(.kind | type == "string" and length > 0)
      | select(.metadata.namespace | type == "string" and length > 0)
      | select(.metadata.name | type == "string" and length > 0)
      | select(.metadata.uid | type == "string" and length > 0)
    ' "$item_partial" >> "$response_items"; then
      bundle_failure=1
      break
    fi
    if ((create_exit != 0)); then
      bundle_failure=1
      break
    fi
  done
  jq -s '{apiVersion:"v1",kind:"List",items:.}' \
    "$response_items" > "$response_partial" || bundle_failure=1
  if [[ -s $response_partial ]]; then
    mv -- "$response_partial" "$response_path" || bundle_failure=1
  fi
  rm -f -- "$response_partial" "$response_items" "$item_partial"
  return "$bundle_failure"
}

uid_cleanup_start_proxy() {
  local kubectl_array_name=$1 temporary_dir=$2 cleanup_log=$3
  local -n cleanup_kubectl=$kubectl_array_name
  local proxy_stdout="$temporary_dir/.cleanup-proxy.stdout"
  local proxy_line proxy_attempt
  UID_CLEANUP_PROXY_PID=""
  UID_CLEANUP_PROXY_URL=""
  : > "$proxy_stdout"
  "${cleanup_kubectl[@]}" proxy --address=127.0.0.1 --port=0 \
    --api-prefix=/ --accept-hosts='^127[.]0[.]0[.]1$' \
    > "$proxy_stdout" 2>> "$cleanup_log" &
  UID_CLEANUP_PROXY_PID=$!
  for ((proxy_attempt=0; proxy_attempt<100; proxy_attempt++)); do
    while IFS= read -r proxy_line; do
      if [[ $proxy_line =~ 127[.]0[.]0[.]1:([0-9]+) ]]; then
        UID_CLEANUP_PROXY_URL="http://127.0.0.1:${BASH_REMATCH[1]}"
        return 0
      fi
    done < "$proxy_stdout"
    kill -0 "$UID_CLEANUP_PROXY_PID" 2>/dev/null || break
    sleep 0.05
  done
  uid_cleanup_stop_proxy
  return 1
}

uid_cleanup_stop_proxy() {
  local stop_attempt
  if [[ -n $UID_CLEANUP_PROXY_PID ]]; then
    if kill -0 "$UID_CLEANUP_PROXY_PID" 2>/dev/null; then
      kill "$UID_CLEANUP_PROXY_PID" 2>/dev/null || true
      for ((stop_attempt=0; stop_attempt<20; stop_attempt++)); do
        kill -0 "$UID_CLEANUP_PROXY_PID" 2>/dev/null || break
        sleep 0.05
      done
      if kill -0 "$UID_CLEANUP_PROXY_PID" 2>/dev/null; then
        kill -KILL "$UID_CLEANUP_PROXY_PID" 2>/dev/null || true
      fi
    fi
    wait "$UID_CLEANUP_PROXY_PID" 2>/dev/null || true
  fi
  UID_CLEANUP_PROXY_PID=""
  UID_CLEANUP_PROXY_URL=""
}

uid_cleanup_append_group_error() {
  local resources_file=$1 group_role=$2 status=$3 create_attempted=$4
  jq -nc \
    --arg group_role "$group_role" \
    --arg status "$status" \
    --argjson create_attempted "$([[ $create_attempted == 1 ]] && printf true || printf false)" \
    '{group_role:$group_role,resource_kind:"group",resource_name:$group_role,
      status:$status,expected_uid:"",observed_uid_before_delete:"",
      create_attempted:$create_attempted,delete_attempted:false,
      uid_precondition_enforced:false,lookup_exit_code:0,delete_exit_code:0,
      wait_exit_code:0}' >> "$resources_file"
}

uid_cleanup_object() {
  local kubectl_array_name=$1 namespace=$2 group_role=$3 object_json=$4
  local timeout=$5 cleanup_log=$6 resources_file=$7 temporary_dir=$8
  local -n cleanup_kubectl=$kubectl_array_name
  local api_version kind resource_kind resource_plural resource_name expected_uid
  local observed_uid="" raw_path="" status="lookup-failed"
  local safe_role
  local lookup_exit_code=0 delete_exit_code=0 wait_exit_code=0
  local delete_attempted=false uid_precondition_enforced=false resource_failure=0

  api_version=$(jq -er '.apiVersion' <<< "$object_json") || return 1
  kind=$(jq -er '.kind' <<< "$object_json") || return 1
  resource_name=$(jq -er '.metadata.name' <<< "$object_json") || return 1
  expected_uid=$(jq -er '.metadata.uid' <<< "$object_json") || return 1
  case "$api_version:$kind" in
    v1:Pod) resource_kind=pod; resource_plural=pods; raw_path=/api/v1 ;;
    v1:Service) resource_kind=service; resource_plural=services; raw_path=/api/v1 ;;
    v1:ConfigMap) resource_kind=configmap; resource_plural=configmaps; raw_path=/api/v1 ;;
    v1:ServiceAccount) resource_kind=serviceaccount; resource_plural=serviceaccounts; raw_path=/api/v1 ;;
    batch/v1:Job) resource_kind=job; resource_plural="jobs"; raw_path=/apis/batch/v1 ;;
    networking.k8s.io/v1:NetworkPolicy)
      resource_kind=networkpolicy; resource_plural=networkpolicies
      raw_path=/apis/networking.k8s.io/v1
      ;;
    rbac.authorization.k8s.io/v1:Role)
      resource_kind=role; resource_plural=roles
      raw_path=/apis/rbac.authorization.k8s.io/v1
      ;;
    rbac.authorization.k8s.io/v1:RoleBinding)
      resource_kind=rolebinding; resource_plural=rolebindings
      raw_path=/apis/rbac.authorization.k8s.io/v1
      ;;
    *) return 1 ;;
  esac
  raw_path="${raw_path}/namespaces/${namespace}/${resource_plural}/${resource_name}"
  safe_role=${group_role}-${resource_kind}-${resource_name}
  safe_role=${safe_role//[^a-zA-Z0-9_.-]/_}
  local delete_options="$temporary_dir/.cleanup-delete-options-${safe_role}.json"
  local delete_response="$temporary_dir/.cleanup-delete-response-${safe_role}.json"
  local current_object="$temporary_dir/.cleanup-current-${safe_role}.json"

  "${cleanup_kubectl[@]}" get "$resource_kind" "$resource_name" \
    --ignore-not-found -o json > "$current_object" 2>> "$cleanup_log"
  lookup_exit_code=$?
  if ((lookup_exit_code != 0)); then
    status="lookup-failed"
    resource_failure=1
  elif [[ ! -s $current_object ]]; then
    status="already-absent"
  elif ! observed_uid=$(jq -er '
    .metadata.uid | select(type == "string" and length > 0)
  ' "$current_object" 2>> "$cleanup_log"); then
    status="lookup-identity-missing"
    resource_failure=1
  elif [[ $observed_uid != "$expected_uid" ]]; then
    status="uid-mismatch-preserved"
    resource_failure=1
  elif [[ -z $UID_CLEANUP_PROXY_URL ]]; then
    status="uid-proxy-unavailable"
    resource_failure=1
  elif ! jq -n \
    --arg uid "$expected_uid" \
    '{apiVersion:"v1",kind:"DeleteOptions",propagationPolicy:"Foreground",
      preconditions:{uid:$uid}}' > "$delete_options"; then
    status="uid-delete-options-failed"
    resource_failure=1
  else
    delete_attempted=true
    uid_precondition_enforced=true
    curl --fail --silent --show-error --max-time 30 \
      --request DELETE --header 'Content-Type: application/json' \
      --data-binary "@$delete_options" \
      "${UID_CLEANUP_PROXY_URL}${raw_path}" \
      > "$delete_response" 2>> "$cleanup_log"
    delete_exit_code=$?
    if ((delete_exit_code != 0)); then
      status="uid-delete-failed"
      resource_failure=1
    else
      "${cleanup_kubectl[@]}" wait --for=delete \
        "$resource_kind/$resource_name" --timeout="$timeout" \
        >> "$cleanup_log" 2>&1
      wait_exit_code=$?
      if ((wait_exit_code != 0)); then
        status="uid-delete-wait-failed"
        resource_failure=1
      else
        status="uid-precondition-deleted"
      fi
    fi
  fi

  jq -nc \
    --arg group_role "$group_role" \
    --arg resource_kind "$resource_kind" \
    --arg resource_name "$resource_name" \
    --arg status "$status" \
    --arg expected_uid "$expected_uid" \
    --arg observed_uid "$observed_uid" \
    --arg delete_transport "kubectl-authenticated-local-proxy" \
    --argjson delete_attempted "$delete_attempted" \
    --argjson uid_precondition_enforced "$uid_precondition_enforced" \
    --argjson lookup_exit_code "$lookup_exit_code" \
    --argjson delete_exit_code "$delete_exit_code" \
    --argjson wait_exit_code "$wait_exit_code" \
    '{group_role:$group_role,resource_kind:$resource_kind,
      resource_name:$resource_name,status:$status,expected_uid:$expected_uid,
      observed_uid_before_delete:$observed_uid,create_attempted:true,
      delete_attempted:$delete_attempted,
      uid_precondition_enforced:$uid_precondition_enforced,
      delete_transport:$delete_transport,
      lookup_exit_code:$lookup_exit_code,delete_exit_code:$delete_exit_code,
      wait_exit_code:$wait_exit_code}' >> "$resources_file" || resource_failure=1
  printf 'group=%s kind=%s name=%s status=%s uid_precondition=%s\n' \
    "$group_role" "$resource_kind" "$resource_name" "$status" \
    "$uid_precondition_enforced" >> "$cleanup_log" || resource_failure=1
  rm -f -- "$delete_options" "$delete_response" "$current_object"
  return "$resource_failure"
}

uid_cleanup_group() {
  local kubectl_array_name=$1 namespace=$2 cleanup_requested=$3 group_role=$4
  local create_response=$5 create_attempted=$6 expected_count=$7 timeout=$8
  local cleanup_log=$9 resources_file=${10} temporary_dir=${11}
  local normalized="$temporary_dir/.cleanup-create-items-${group_role}.ndjson"
  local actual_count=0 group_failure=0 object_json object_index
  local -a cleanup_objects=()

  if ((cleanup_requested == 0)); then
    uid_cleanup_append_group_error \
      "$resources_file" "$group_role" not-requested "$create_attempted"
    return $?
  fi
  if ((create_attempted == 0)); then
    uid_cleanup_append_group_error \
      "$resources_file" "$group_role" not-created "$create_attempted"
    return $?
  fi
  if [[ ! -f $create_response || -L $create_response ]] || ! jq -ce \
    --arg namespace "$namespace" '
      (if .kind == "List" then .items[] else . end)
      | select(type == "object")
      | select(.apiVersion | type == "string" and length > 0)
      | select(.kind | type == "string" and length > 0)
      | select(.metadata.namespace == $namespace)
      | select(.metadata.name | type == "string" and length > 0)
      | select(.metadata.uid | type == "string" and length > 0)
    ' "$create_response" > "$normalized" 2>> "$cleanup_log"; then
    uid_cleanup_append_group_error \
      "$resources_file" "$group_role" uid-receipt-missing "$create_attempted" || true
    rm -f -- "$normalized"
    return 1
  fi
  actual_count=$(wc -l < "$normalized")
  if ((actual_count != expected_count)) || ! jq -s -e \
    --arg group_role "$group_role" '
      ([.[] | [.apiVersion,.kind,.metadata.namespace,.metadata.name,.metadata.uid]
        | join("|")] | length == (unique | length)) and
      (if $group_role == "target" then
         ([.[] | .kind] | sort) == ["Pod"]
       elif $group_role == "target-support" then
         ([.[] | .kind] | sort) == ["NetworkPolicy","NetworkPolicy","Service","Service"]
       elif $group_role == "restore-worker" then
         ([.[] | .kind] | sort) == ["Job","Role","RoleBinding","ServiceAccount"]
       elif $group_role == "semantic-probe" then
         ([.[] | .kind] | sort) == ["ConfigMap","Job"]
       else false end)
    ' "$normalized" >/dev/null; then
    uid_cleanup_append_group_error \
      "$resources_file" "$group_role" uid-receipt-incomplete "$create_attempted" || true
    group_failure=1
  fi

  # Reverse creation order so Jobs/Pods are normally removed before their
  # supporting RBAC, ConfigMaps, Services, and policies.
  mapfile -t cleanup_objects < "$normalized"
  for ((object_index=${#cleanup_objects[@]} - 1; object_index >= 0; object_index--)); do
    object_json=${cleanup_objects[$object_index]}
    uid_cleanup_object "$kubectl_array_name" "$namespace" "$group_role" \
      "$object_json" "$timeout" "$cleanup_log" "$resources_file" \
      "$temporary_dir" || group_failure=1
  done
  rm -f -- "$normalized"
  return "$group_failure"
}
