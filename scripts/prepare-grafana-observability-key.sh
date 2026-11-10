#!/usr/bin/env bash

# This helper is sourced by environment.sh so its TF_VAR exports remain in the
# operator's shell. It intentionally does not revoke previous keys; replaced
# keys remain valid until their configured expiry.

_grafana_observability_epoch() {
  local timestamp=$1
  if [[ "$(uname)" == "Darwin" ]]; then
    date -j -u -f '%Y-%m-%dT%H:%M:%SZ' "${timestamp}" '+%s' 2>/dev/null
  else
    date -u -d "${timestamp}" '+%s' 2>/dev/null
  fi
}

_grafana_observability_expiry() {
  if [[ "$(uname)" == "Darwin" ]]; then
    date -u -v+6m '+%Y-%m-%dT%H:%M:%SZ'
  else
    date -u -d '+6 months' '+%Y-%m-%dT%H:%M:%SZ'
  fi
}

_grafana_observability_remove_legacy_file() {
  local legacy_file="${PWD}/grafana_access_token.txt"
  if [[ -f "${legacy_file}" ]]; then
    if ! rm -f -- "${legacy_file}"; then
      echo "Error: failed to remove legacy Grafana token file: ${legacy_file}" >&2
      return 1
    fi
    echo "Removed legacy Grafana token file: ${legacy_file}"
  fi
}

_grafana_observability_discard_issued_key() {
  local key_id=$1 token=$2

  if [[ -n "${key_id}" ]] && nebius iam static-key delete --id "${key_id}" >/dev/null 2>&1; then
    echo "Removed newly issued Grafana Observability key ${key_id} after credential preparation failed." >&2
    return 0
  fi
  if [[ -n "${token}" ]] && printf '%s\n' "${token}" | jq -Rcn '{token: input}' | \
    nebius iam static-key revoke --file /dev/stdin >/dev/null 2>&1; then
    echo "Revoked the newly issued Grafana Observability key after credential preparation failed." >&2
    return 0
  fi

  echo "Error: failed to remove the newly issued Grafana Observability key after credential preparation failed." >&2
  return 1
}

_prepare_grafana_observability_key() {
  local key_file now_epoch rotate_before_epoch cached_expiry cached_expiry_epoch
  local response token key_id key_name lookup_response expires_at temporary_file previous_umask
  local cached_project_id cached_service_account_id
  local remote_response remote_key_id remote_active remote_status
  local project_id service_account_id cache_namespace

  for command in jq nebius date uname; do
    if ! command -v "${command}" >/dev/null 2>&1; then
      echo "Error: ${command} is required to prepare the Grafana Observability key." >&2
      return 1
    fi
  done

  project_id=${GRAFANA_OBSERVABILITY_PROJECT_ID:-${NEBIUS_PROJECT_ID:-}}
  service_account_id=${GRAFANA_OBSERVABILITY_SERVICE_ACCOUNT_ID:-${NEBIUS_SA_ID:-}}
  cache_namespace=${GRAFANA_OBSERVABILITY_CACHE_NAMESPACE:-${TF_VAR_cluster_name:-}}

  if [[ -z "${project_id}" || -z "${service_account_id}" || -z "${cache_namespace}" ]]; then
    echo "Error: project, service-account, and cluster identifiers must be set before preparing the Grafana Observability key." >&2
    return 1
  fi
  if [[ ! "${cache_namespace}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    echo "Error: Grafana Observability cache namespace must use letters, digits, dots, underscores, or hyphens." >&2
    return 1
  fi

  key_file=${GRAFANA_OBSERVABILITY_KEY_FILE:-"${PWD}/.grafana_observability_key.${cache_namespace}.json"}
  now_epoch=$(date -u '+%s') || return 1
  rotate_before_epoch=$((now_epoch + 30 * 24 * 60 * 60))

  if [[ -f "${key_file}" ]]; then
    if ! chmod 600 "${key_file}"; then
      echo "Error: failed to enforce owner-only permissions on ${key_file}." >&2
      return 1
    fi
    if ! jq -e '
      type == "object"
      and (.project_id | type == "string" and length > 0)
      and (.service_account_id | type == "string" and length > 0)
      and (.key_id | type == "string" and length > 0)
      and (.token | type == "string" and length > 0)
      and (.expires_at | type == "string" and length > 0)' \
      "${key_file}" >/dev/null 2>&1; then
      echo "Error: Grafana Observability key cache is malformed: ${key_file}" >&2
      return 1
    fi

    cached_project_id=$(jq -r '.project_id' "${key_file}")
    cached_service_account_id=$(jq -r '.service_account_id' "${key_file}")
    if [[ "${cached_project_id}" != "${project_id}" || "${cached_service_account_id}" != "${service_account_id}" ]]; then
      echo "Grafana Observability key cache belongs to another project or service account; issuing a key for the current context."
    else
      cached_expiry=$(jq -r '.expires_at' "${key_file}")
      if ! cached_expiry_epoch=$(_grafana_observability_epoch "${cached_expiry}"); then
        echo "Error: Grafana Observability key cache has an invalid expiry: ${key_file}" >&2
        return 1
      fi
      if [[ "${cached_expiry_epoch}" -gt "${rotate_before_epoch}" ]]; then
        key_id=$(jq -r '.key_id' "${key_file}")
        token=$(jq -r '.token' "${key_file}")
        if remote_response=$(printf '%s\n' "${token}" | jq -Rcn '{token: input}' | \
          nebius iam static-key find --file /dev/stdin --format json 2>/dev/null); then
          if ! remote_key_id=$(jq -er '
            (.staticKey // .static_key // .) as $key
            | $key.metadata.id
            | select(type == "string" and length > 0)' <<<"${remote_response}"); then
            echo "Error: Nebius returned an invalid response while validating cached Grafana key ${key_id}." >&2
            return 1
          fi
          remote_active=$(jq -r '
            (.staticKey // .static_key // .) as $key
            | if (($key.status | type) == "object"
                and ($key.status | has("active"))
                and (($key.status.active | type) == "boolean"))
              then $key.status.active
              else empty
              end' <<<"${remote_response}")
          if [[ "${remote_active}" != "true" && "${remote_active}" != "false" ]]; then
            echo "Error: Nebius did not return the status of cached Grafana key ${key_id}." >&2
            return 1
          fi
          if [[ "${remote_key_id}" != "${key_id}" ]]; then
            echo "Error: cached Grafana key ID ${key_id} does not match remotely resolved key ${remote_key_id}." >&2
            return 1
          fi
          if [[ "${remote_active}" == "true" ]]; then
            export TF_VAR_grafana_observability_key_id="${key_id}"
            export TF_VAR_grafana_observability_access_token="${token}"
            export GRAFANA_OBSERVABILITY_KEY_EXPIRES_AT="${cached_expiry}"
            _grafana_observability_remove_legacy_file || return 1
            echo "Using existing Grafana Observability key: ${TF_VAR_grafana_observability_key_id}"
            return 0
          fi
          echo "Cached Grafana Observability key ${key_id} is inactive; issuing a replacement."
        else
          remote_status=$?
          if [[ "${remote_status}" -ne 13 ]]; then
            echo "Error: failed to validate cached Grafana Observability key ${key_id} (exit ${remote_status})." >&2
            return "${remote_status}"
          fi
          echo "Cached Grafana Observability key ${key_id} no longer exists; issuing a replacement."
        fi
      fi
    fi
  fi

  expires_at=$(_grafana_observability_expiry) || return 1
  key_name="${cache_namespace}.grafana-observability-$(date -u '+%Y%m%dT%H%M%SZ')"
  if ! response=$(nebius iam static-key issue \
    --parent-id "${project_id}" \
    --account-service-account-id "${service_account_id}" \
    --service observability \
    --name "${key_name}" \
    --expires-at "${expires_at}" \
    --format json); then
    echo "Error: failed to issue the Grafana Observability key." >&2
    return 1
  fi

  token=$(jq -r '
    (.token // .status.token // .secret // empty)
    | select(type == "string" and length > 0)' <<<"${response}")
  key_id=$(jq -r '
    (.metadata.id
     // .id
     // .operation.resource_id
     // .operation.resourceId
     // empty)
    | select(type == "string" and length > 0)' <<<"${response}")

  if [[ -z "${key_id}" ]]; then
    if lookup_response=$(nebius iam static-key get-by-name \
      --parent-id "${service_account_id}" \
      --name "${key_name}" \
      --format json 2>/dev/null); then
      key_id=$(jq -r '
        (.staticKey // .static_key // .) as $key
        | ($key.metadata.id // $key.id // empty)
        | select(type == "string" and length > 0)' <<<"${lookup_response}")
    fi
  fi

  if [[ -z "${token}" ]]; then
    _grafana_observability_discard_issued_key "${key_id}" "" || true
    echo "Error: the issued Grafana Observability key did not contain a token." >&2
    return 1
  fi
  if [[ -z "${key_id}" ]]; then
    _grafana_observability_discard_issued_key "" "${token}" || true
    echo "Error: failed to resolve the ID of the issued Grafana Observability key." >&2
    return 1
  fi

  temporary_file="${key_file}.tmp.$$"
  previous_umask=$(umask)
  umask 077
  if ! printf '%s\n' \
    "${project_id}" \
    "${service_account_id}" \
    "${key_id}" \
    "${token}" \
    "${expires_at}" | jq -Rn '
      [inputs] as $values
      | {project_id: $values[0], service_account_id: $values[1],
         key_id: $values[2], token: $values[3], expires_at: $values[4]}' \
      >"${temporary_file}"; then
    umask "${previous_umask}"
    rm -f "${temporary_file}"
    _grafana_observability_discard_issued_key "${key_id}" "${token}" || true
    return 1
  fi
  if ! chmod 600 "${temporary_file}"; then
    umask "${previous_umask}"
    rm -f "${temporary_file}"
    _grafana_observability_discard_issued_key "${key_id}" "${token}" || true
    return 1
  fi
  if ! mv "${temporary_file}" "${key_file}"; then
    umask "${previous_umask}"
    rm -f "${temporary_file}"
    _grafana_observability_discard_issued_key "${key_id}" "${token}" || true
    return 1
  fi
  umask "${previous_umask}"

  export TF_VAR_grafana_observability_key_id="${key_id}"
  export TF_VAR_grafana_observability_access_token="${token}"
  export GRAFANA_OBSERVABILITY_KEY_EXPIRES_AT="${expires_at}"
  _grafana_observability_remove_legacy_file || return 1
  echo "Created Grafana Observability key: ${key_id}"
}

_prepare_grafana_observability_key
_grafana_observability_status=$?
unset -f _grafana_observability_epoch _grafana_observability_expiry _grafana_observability_remove_legacy_file _grafana_observability_discard_issued_key _prepare_grafana_observability_key
return "${_grafana_observability_status}" 2>/dev/null || exit "${_grafana_observability_status}"
