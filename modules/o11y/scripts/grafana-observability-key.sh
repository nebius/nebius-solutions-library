#!/usr/bin/env bash

case "${1:-}" in
  issue)
    set -euo pipefail
    umask 077

    TOKEN_TMP=""
    METADATA_TMP=""
    PREVIOUS_TOKEN_TMP=""
    PREVIOUS_METADATA_TMP=""
    NEW_TOKEN=""
    NEW_KEY_ID=""
    HAD_CURRENT=false
    HAD_CURRENT_METADATA=false
    COMMITTED=false

    cleanup() {
      status=$?
      trap - EXIT

      for file in "$TOKEN_TMP" "$METADATA_TMP" "$PREVIOUS_TOKEN_TMP" "$PREVIOUS_METADATA_TMP"; do
        if [ -n "$file" ]; then
          rm -f -- "$file"
        fi
      done

      if [ "$COMMITTED" != true ] && { [ -n "$NEW_TOKEN" ] || [ -n "$NEW_KEY_ID" ]; }; then
        echo "Token setup failed; invalidating the newly issued key" >&2
        if [ -n "$NEW_KEY_ID" ]; then
          nebius iam static-key delete --id "$NEW_KEY_ID" >/dev/null || \
            echo "Warning: failed to delete newly issued key $NEW_KEY_ID" >&2
        else
          nebius iam static-key revoke --token "$NEW_TOKEN" >/dev/null || \
            echo "Warning: failed to revoke newly issued key" >&2
        fi

        if [ "$HAD_CURRENT" = true ]; then
          cp -- "$PREVIOUS_TOKEN_FILE" "$CURRENT_TOKEN_FILE"
          chmod 600 "$CURRENT_TOKEN_FILE"
          if [ "$HAD_CURRENT_METADATA" = true ]; then
            cp -- "$PREVIOUS_METADATA_FILE" "$CURRENT_METADATA_FILE"
            chmod 600 "$CURRENT_METADATA_FILE"
          else
            rm -f -- "$CURRENT_METADATA_FILE"
          fi
        else
          rm -f -- "$CURRENT_TOKEN_FILE" "$CURRENT_METADATA_FILE"
        fi
      fi

      exit "$status"
    }
    trap cleanup EXIT

    for command in nebius jq mktemp; do
      command -v "$command" >/dev/null || {
        echo "Required command not found: $command" >&2
        exit 1
      }
    done

    # A completed rotation must be finalized before another one can start. A
    # previous token identical to the current token is allowed because it represents
    # a retry after an earlier token-generation failure.
    if [ -e "$PREVIOUS_TOKEN_FILE" ] || [ -e "$PREVIOUS_METADATA_FILE" ]; then
      if [ ! -s "$PREVIOUS_TOKEN_FILE" ] || [ ! -s "$PREVIOUS_METADATA_FILE" ]; then
        echo "Incomplete previous Grafana credential backup; resolve it before rotating again" >&2
        exit 1
      fi
      if [ ! -s "$CURRENT_TOKEN_FILE" ] || ! cmp -s "$CURRENT_TOKEN_FILE" "$PREVIOUS_TOKEN_FILE"; then
        echo "A previous Grafana credential is awaiting validation and revocation; finalize it before rotating again" >&2
        exit 1
      fi
    fi

    # A clean first apply has neither local credentials nor an active
    # Observability key for this service account. If an active remote key exists,
    # missing local files mean this is another machine/runner and replacing the
    # generator would orphan the credential that Grafana currently uses.
    if [ ! -e "$CURRENT_TOKEN_FILE" ] && [ ! -e "$CURRENT_METADATA_FILE" ]; then
      if ! EXISTING_KEYS_JSON=$(nebius iam static-key list \
        --parent-id "$PARENT_ID" \
        --all \
        --format json); then
        echo "Failed to check for an existing Grafana Observability key" >&2
        exit 1
      fi

      EXISTING_KEY_ID=$(printf '%s' "$EXISTING_KEYS_JSON" | jq -r --arg SA "$SERVICE_ACCOUNT_ID" '
        def items: if type == "array" then . else (.items // []) end;
        [items[]
          | select(.spec.service == "OBSERVABILITY")
          | select(.spec.account.service_account.id == $SA)
          | select(.status.active == true)]
        | first
        | .metadata.id // empty')

      if [ -n "$EXISTING_KEY_ID" ]; then
        echo "Cannot replace Grafana Observability key: active key $EXISTING_KEY_ID exists but the local credential files are missing" >&2
        echo "Run the rotation from the original machine/runner or recover the current credential files first" >&2
        exit 1
      fi
    fi

    # Preserve the active credential before issuing its replacement. Deployments
    # created by older versions may not have metadata; retain a legacy marker so the
    # old token can still be revoked after validation.
    if [ -e "$CURRENT_TOKEN_FILE" ] || [ -e "$CURRENT_METADATA_FILE" ]; then
      if [ ! -s "$CURRENT_TOKEN_FILE" ]; then
        echo "Current Grafana token file is missing or empty: $CURRENT_TOKEN_FILE" >&2
        exit 1
      fi

      HAD_CURRENT=true
      PREVIOUS_TOKEN_TMP="$(mktemp "${PREVIOUS_TOKEN_FILE}.tmp.XXXXXX")"
      PREVIOUS_METADATA_TMP="$(mktemp "${PREVIOUS_METADATA_FILE}.tmp.XXXXXX")"
      cp -- "$CURRENT_TOKEN_FILE" "$PREVIOUS_TOKEN_TMP"

      if [ -s "$CURRENT_METADATA_FILE" ]; then
        HAD_CURRENT_METADATA=true
        cp -- "$CURRENT_METADATA_FILE" "$PREVIOUS_METADATA_TMP"
      else
        jq -n '{id: null, name: null, legacy: true}' > "$PREVIOUS_METADATA_TMP"
      fi

      chmod 600 "$PREVIOUS_TOKEN_TMP" "$PREVIOUS_METADATA_TMP"
      mv -f "$PREVIOUS_TOKEN_TMP" "$PREVIOUS_TOKEN_FILE"
      PREVIOUS_TOKEN_TMP=""
      mv -f "$PREVIOUS_METADATA_TMP" "$PREVIOUS_METADATA_FILE"
      PREVIOUS_METADATA_TMP=""
      echo "Preserved the previous Grafana Observability credential for post-rotation validation"
    fi

    # Generate access token using the static key
    echo "Generating Observability access token for service account: $SERVICE_ACCOUNT_ID"
    KEY_NAME="grafana-observability-$(date -u +%Y%m%dT%H%M%SZ)-$$"
    ISSUE_RESPONSE=$(nebius iam static-key issue \
      --parent-id "$PARENT_ID" \
      --account-service-account-id "$SERVICE_ACCOUNT_ID" \
      --service observability \
      --name "$KEY_NAME" \
      --format json)

    NEW_TOKEN=$(printf '%s' "$ISSUE_RESPONSE" | jq -r '.token // empty')
    NEW_KEY_ID=$(printf '%s' "$ISSUE_RESPONSE" | jq -r '.metadata.id // .id // .static_key.metadata.id // .resource.metadata.id // empty')

    # Some CLI versions return the token without the completed resource metadata.
    # Resolve the non-secret key ID by the unique name when it is absent.
    if [ -z "$NEW_KEY_ID" ]; then
      KEY_RESPONSE=$(nebius iam static-key get-by-name \
        --parent-id "$PARENT_ID" \
        --name "$KEY_NAME" \
        --format json)
      NEW_KEY_ID=$(printf '%s' "$KEY_RESPONSE" | jq -r '.metadata.id // .id // empty')
    fi

    if [ -z "$NEW_TOKEN" ]; then
      echo "Failed to generate access token" >&2
      exit 1
    fi

    if [ -z "$NEW_KEY_ID" ]; then
      echo "Failed to resolve the newly generated static-key ID" >&2
      exit 1
    fi

    TOKEN_TMP="$(mktemp "${CURRENT_TOKEN_FILE}.tmp.XXXXXX")"
    METADATA_TMP="$(mktemp "${CURRENT_METADATA_FILE}.tmp.XXXXXX")"
    printf '%s\n' "$NEW_TOKEN" > "$TOKEN_TMP"
    jq -n \
      --arg id "$NEW_KEY_ID" \
      --arg name "$KEY_NAME" \
      --arg issued_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      '{id: $id, name: $name, issued_at: $issued_at}' > "$METADATA_TMP"
    chmod 600 "$TOKEN_TMP" "$METADATA_TMP"
    mv -f "$METADATA_TMP" "$CURRENT_METADATA_FILE"
    METADATA_TMP=""
    mv -f "$TOKEN_TMP" "$CURRENT_TOKEN_FILE"
    TOKEN_TMP=""

    COMMITTED=true
    trap - EXIT
    echo "Access token generated successfully"
    echo "Current key ID: $NEW_KEY_ID"
    if [ "$HAD_CURRENT" = true ]; then
      echo "After validating all Grafana datasources, delete the previous key and its local backup as documented"
    fi
    ;;
  cleanup)
    set -euo pipefail

    already_invalidated() {
      printf '%s\n' "$1" | grep -Eiq 'ResourceNotFound|NotFound|not found|already (deleted|revoked)|has already been (deleted|revoked)'
    }

    delete_credential() {
      metadata_file="$1"
      token_file="$2"
      description="$3"
      key_id=""
      command_output=""

      if [ -s "$metadata_file" ]; then
        key_id=$(jq -r '.id // empty' "$metadata_file")
      fi

      if [ -n "$key_id" ]; then
        echo "Deleting $description Grafana Observability key: $key_id"
        if ! command_output=$(nebius iam static-key delete --id "$key_id" 2>&1); then
          if already_invalidated "$command_output"; then
            echo "$description Grafana Observability key is already absent: $key_id"
          else
            echo "Failed to delete $description Grafana Observability key $key_id; local credential files were retained" >&2
            return 1
          fi
        fi
      elif [ -s "$token_file" ]; then
        echo "Revoking $description legacy Grafana Observability key"
        if ! command_output=$(nebius iam static-key revoke --token "$(cat "$token_file")" 2>&1); then
          if already_invalidated "$command_output"; then
            echo "$description legacy Grafana Observability key is already revoked"
          else
            echo "Failed to revoke $description legacy Grafana Observability key; local credential files were retained" >&2
            return 1
          fi
        fi
      elif [ -e "$metadata_file" ] || [ -e "$token_file" ]; then
        echo "Cannot invalidate the $description Grafana Observability key: its local credential files are incomplete" >&2
        return 1
      fi
    }

    if [ ! -s "$CURRENT_METADATA_FILE" ] && [ ! -s "$CURRENT_TOKEN_FILE" ]; then
      echo "Cannot invalidate the current Grafana Observability key: local credential metadata and token are both missing" >&2
      exit 1
    fi

    delete_credential "$CURRENT_METADATA_FILE" "$CURRENT_TOKEN_FILE" "current"

    current_id=$(jq -r '.id // empty' "$CURRENT_METADATA_FILE" 2>/dev/null || true)
    previous_id=$(jq -r '.id // empty' "$PREVIOUS_METADATA_FILE" 2>/dev/null || true)
    same_credential=false
    if [ -n "$current_id" ] && [ "$current_id" = "$previous_id" ]; then
      same_credential=true
    elif [ -s "$CURRENT_TOKEN_FILE" ] && [ -s "$PREVIOUS_TOKEN_FILE" ] && cmp -s "$CURRENT_TOKEN_FILE" "$PREVIOUS_TOKEN_FILE"; then
      same_credential=true
    fi

    if [ "$same_credential" != true ]; then
      delete_credential "$PREVIOUS_METADATA_FILE" "$PREVIOUS_TOKEN_FILE" "previous"
    fi

    rm -f -- "$CURRENT_TOKEN_FILE" "$CURRENT_METADATA_FILE" "$PREVIOUS_TOKEN_FILE" "$PREVIOUS_METADATA_FILE"
    ;;
  *)
    echo "Usage: $0 {issue|cleanup}" >&2
    exit 2
    ;;
esac
