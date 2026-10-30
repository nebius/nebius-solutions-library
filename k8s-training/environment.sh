#!/bin/bash

# Set the following environment variables:
# NEBIUS_TENANT_ID='tenant-...'
# NEBIUS_PROJECT_ID='project-...'
# NEBIUS_REGION='eu-west2'

# Match Terraform's default without parsing HCL or requiring a local tfvars file.
TF_VAR_cluster_name="${TF_VAR_cluster_name:-k8s-training}"
if [[ ! "${TF_VAR_cluster_name}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
  echo "Error: export TF_VAR_cluster_name with a non-empty cluster name (letters, digits, dots, underscores, or hyphens)." >&2
  return 1
fi
export TF_VAR_cluster_name

# Preserve the historical key, including for existing clusters with custom names.
# Additional clusters must explicitly choose a different K8S_TF_STATE_KEY.
K8S_RESOLVED_TF_STATE_KEY="${K8S_TF_STATE_KEY-k8s-training.tfstate}"
if [[ ! "${K8S_RESOLVED_TF_STATE_KEY}" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*\.tfstate$ ]]; then
  echo "Error: K8S_TF_STATE_KEY must be a non-empty key ending in .tfstate, using letters, digits, dots, underscores, hyphens, or slashes." >&2
  return 1
fi

case "/${K8S_RESOLVED_TF_STATE_KEY}/" in
  */./*|*/../*)
    echo "Error: K8S_TF_STATE_KEY must not contain standalone . or .. path segments." >&2
    return 1
    ;;
esac

if [ -z "${NEBIUS_TENANT_ID}" ]; then
  echo "Error: NEBIUS_TENANT_ID is not set"
  return 1
fi

if [ -z "${NEBIUS_PROJECT_ID}" ]; then
  echo "Error: NEBIUS_PROJECT_ID is not set"
  return 1
fi

if [ -z "${NEBIUS_REGION}" ]; then
  echo "Error: NEBIUS_REGION is not set"
  return 1
fi

# IAM token
unset NEBIUS_IAM_TOKEN
export NEBIUS_IAM_TOKEN=$(nebius iam get-access-token)

# VPC subnet: honor an explicit ID, otherwise prefer subnets in default* networks.
if [ -n "${NEBIUS_VPC_SUBNET_ID:-}" ]; then
  echo "Using configured subnet: ${NEBIUS_VPC_SUBNET_ID}"
else
  if ! K8S_VPC_NETWORKS_JSON=$(nebius vpc network list \
    --parent-id "${NEBIUS_PROJECT_ID}" --all --format json); then
    echo "Error: failed to list networks in project ${NEBIUS_PROJECT_ID}" >&2
    return 1
  fi
  if ! K8S_VPC_SUBNETS_JSON=$(nebius vpc subnet list \
    --parent-id "${NEBIUS_PROJECT_ID}" --all --format json); then
    echo "Error: failed to list subnets in project ${NEBIUS_PROJECT_ID}" >&2
    return 1
  fi

  # Accept list envelopes and arrays. Prefer a default* subnet in a default*
  # network, then any subnet in those networks, then the first project subnet.
  if ! NEBIUS_VPC_SUBNET_ID=$(jq -rs --argjson NETWORKS "${K8S_VPC_NETWORKS_JSON}" '
    def items: if type == "array" then . else (.items // []) end;
    # Reject empty responses and JSON streams before selecting a single subnet.
    if length == 1 then .[0] else error("expected one subnet list response") end
    | ($NETWORKS | items
      | map(select((.metadata.name // "") | startswith("default"))
        | .metadata.id // empty)) as $default_network_ids
    | items as $subnets
    | ($subnets | map(select(.spec.network_id as $network_id
        | $default_network_ids | index($network_id)))) as $network_subnets
    | (($network_subnets
        | map(select((.metadata.name // "") | startswith("default"))) | .[0])
      // $network_subnets[0] // $subnets[0])
    | .metadata.id // empty' <<< "${K8S_VPC_SUBNETS_JSON}"); then
    echo "Error: failed to parse VPC network/subnet responses in project ${NEBIUS_PROJECT_ID}" >&2
    return 1
  fi
  if [ -z "${NEBIUS_VPC_SUBNET_ID}" ]; then
    echo "Error: no subnet found in project ${NEBIUS_PROJECT_ID}; create one or set NEBIUS_VPC_SUBNET_ID" >&2
    return 1
  fi
  echo "Using discovered subnet: ${NEBIUS_VPC_SUBNET_ID}"
  unset K8S_VPC_NETWORKS_JSON K8S_VPC_SUBNETS_JSON
fi
export NEBIUS_VPC_SUBNET_ID

# Object Storage Bucket
export NEBIUS_BUCKET_NAME="tfstate-k8s-training-$(echo -n "${NEBIUS_TENANT_ID}-${NEBIUS_PROJECT_ID}" | md5sum | awk '$0=$1')"
NEBIUS_BUCKET_ID=$(nebius storage bucket list \
  --parent-id "${NEBIUS_PROJECT_ID}" \
  --format json \
  | jq -r --arg BUCKET "${NEBIUS_BUCKET_NAME}" 'try .items[] | select(.metadata.name == $BUCKET) | .metadata.id')
if [ -z "${NEBIUS_BUCKET_ID}" ]; then
  NEBIUS_BUCKET_ID=$(nebius storage bucket create \
    --name "${NEBIUS_BUCKET_NAME}" \
    --parent-id "${NEBIUS_PROJECT_ID}" \
    --versioning-policy 'enabled' --format json | jq -er '.metadata.id // empty') || return
  echo "Created bucket: ${NEBIUS_BUCKET_NAME}"
else
  echo "Using existing bucket: ${NEBIUS_BUCKET_NAME}"
fi

# Nebius service account
NEBIUS_SA_NAME="k8s-training-tfstate-sa"

if NEBIUS_SA_ID=$(nebius iam service-account get-by-name \
  --parent-id "$NEBIUS_PROJECT_ID" \
  --name "$NEBIUS_SA_NAME" \
  --format 'jsonpath=.metadata.id'); then
  NEBIUS_SA_ACTION="Using existing"
else
  NEBIUS_SA_LOOKUP_EXIT_CODE=$?

  # Nebius CLI exit code 13 means Not Found. Stop on any other lookup error.
  if [ "$NEBIUS_SA_LOOKUP_EXIT_CODE" -ne 13 ]; then
    return "$NEBIUS_SA_LOOKUP_EXIT_CODE"
  fi

  if NEBIUS_SA_ID=$(nebius iam service-account create \
    --parent-id "$NEBIUS_PROJECT_ID" \
    --name "$NEBIUS_SA_NAME" \
    --format 'jsonpath=.metadata.id'); then
    NEBIUS_SA_ACTION="Created"
  else
    return $?
  fi
fi

if [ -z "$NEBIUS_SA_ID" ]; then
  echo "Error: service-account command returned an empty ID." >&2
  return 1
fi

echo "$NEBIUS_SA_ACTION service account: $NEBIUS_SA_ID"

# This service account accesses only the Terraform state bucket.
if NEBIUS_STATE_GROUP_ID=$(nebius iam group get-by-name \
  --parent-id "${NEBIUS_PROJECT_ID}" --name "${NEBIUS_SA_NAME}-state" \
  --format 'jsonpath=.metadata.id'); then
  :
else
  NEBIUS_GROUP_LOOKUP_EXIT_CODE=$?
  [ "$NEBIUS_GROUP_LOOKUP_EXIT_CODE" -eq 13 ] || return "$NEBIUS_GROUP_LOOKUP_EXIT_CODE"
  NEBIUS_STATE_GROUP_ID=$(nebius iam group create \
    --parent-id "${NEBIUS_PROJECT_ID}" --name "${NEBIUS_SA_NAME}-state" \
    --format 'jsonpath=.metadata.id') || return
fi
NEBIUS_STATE_PERMITS=$(nebius iam access-permit list \
  --parent-id "${NEBIUS_STATE_GROUP_ID}" --all --format json) || return
for NEBIUS_STATE_ROLE in storage.object-editor storage.viewer; do
  if ! jq -e --arg role "$NEBIUS_STATE_ROLE" --arg bucket "$NEBIUS_BUCKET_ID" \
    '.items[]? | select(.spec.role == $role and .spec.resource_id == $bucket)' \
    <<< "$NEBIUS_STATE_PERMITS" >/dev/null; then
    nebius iam access-permit create --parent-id "${NEBIUS_STATE_GROUP_ID}" \
      --resource-id "$NEBIUS_BUCKET_ID" --role "$NEBIUS_STATE_ROLE" >/dev/null || return
  fi
done
NEBIUS_STATE_MEMBERS=$(nebius iam group-membership list-members \
  --parent-id "${NEBIUS_STATE_GROUP_ID}" --page-size 1000 --format json) || return
IS_MEMBER=$(jq -r --arg SAID "${NEBIUS_SA_ID}" \
  '.memberships[]? | select(.spec.member_id == $SAID) | .spec.member_id' \
  <<< "$NEBIUS_STATE_MEMBERS") || return
if [ -z "${IS_MEMBER}" ]; then
  RESPONSE=$(nebius iam group-membership create \
    --parent-id "${NEBIUS_STATE_GROUP_ID}" \
    --member-id "${NEBIUS_SA_ID}") || return
  echo "Added service account to state bucket group"
else
  echo "Service account is already a member of state bucket group"
fi

# Nebius service account access key
DATE_FORMAT='+%Y-%m-%dT%H:%M:%SZ'
if [[ "$(uname)" == "Darwin" ]]; then
  # macOS
  EXPIRATION_DATE=$(date -v +1d "${DATE_FORMAT}")
else
  # Linux (assumes GNU date)
  EXPIRATION_DATE=$(date -d '+1 day' "${DATE_FORMAT}")
fi
NEBIUS_SA_ACCESS_KEY_ID=$(nebius iam v2 access-key create \
  --parent-id "${NEBIUS_PROJECT_ID}" \
  --name "tfstate-${TF_VAR_cluster_name}-$(date +%s)" \
  --account-service-account-id "${NEBIUS_SA_ID}" \
  --description 'Temporary Object Storage Access for Terraform' \
  --expires-at "${EXPIRATION_DATE}" \
  --format json \
  | jq -r '.metadata.id')
echo "Created new access key: ${NEBIUS_SA_ACCESS_KEY_ID}"

# AWS-compatible access key
export AWS_ACCESS_KEY_ID=$(nebius iam v2 access-key get \
  --id "${NEBIUS_SA_ACCESS_KEY_ID}" \
  --format json | jq -r '.status.aws_access_key_id')
export AWS_SECRET_ACCESS_KEY=$(nebius iam v2 access-key get \
  --id "${NEBIUS_SA_ACCESS_KEY_ID}" \
  --format json \
  | jq -r '.status.secret')

# Show the exact state location before writing the backend configuration.
printf 'Terraform state: s3://%s/%s\n' "$NEBIUS_BUCKET_NAME" "$K8S_RESOLVED_TF_STATE_KEY"
if [[ "$TF_VAR_cluster_name" != "k8s-training" && -z "${K8S_TF_STATE_KEY+x}" ]]; then
  printf "Warning: cluster name is '%s' but state key is the legacy default; set K8S_TF_STATE_KEY for an additional cluster.\n" "$TF_VAR_cluster_name" >&2
fi

# Use Object Storage as Terraform backend
cat > terraform_backend_override.tf << EOF
terraform {
  backend "s3" {
    bucket = "${NEBIUS_BUCKET_NAME}"
    key    = "${K8S_RESOLVED_TF_STATE_KEY}"

    endpoints = {
      s3 = "https://storage.${NEBIUS_REGION}.nebius.cloud:443"
    }
    region = "${NEBIUS_REGION}"

    skip_region_validation      = true
    skip_credentials_validation = true
    skip_requesting_account_id  = true
    skip_s3_checksum            = true
  }
}
EOF

# Terraform variables
export TF_VAR_iam_token="${NEBIUS_IAM_TOKEN}"
export TF_VAR_parent_id="${NEBIUS_PROJECT_ID}"
export TF_VAR_region="${NEBIUS_REGION}"
export TF_VAR_subnet_id="${NEBIUS_VPC_SUBNET_ID}"
export TF_VAR_tenant_id="${NEBIUS_TENANT_ID}"

# Exported variables
echo "Exported variables:"
echo "NEBIUS_TENANT_ID: ${NEBIUS_TENANT_ID}"
echo "NEBIUS_PROJECT_ID: ${NEBIUS_PROJECT_ID}"
echo "NEBIUS_REGION: ${NEBIUS_REGION}"
echo "NEBIUS_VPC_SUBNET_ID: ${NEBIUS_VPC_SUBNET_ID}"
echo "NEBIUS_BUCKET_NAME: ${NEBIUS_BUCKET_NAME}"
echo "AWS_ACCESS_KEY_ID: ${AWS_ACCESS_KEY_ID}"
echo "AWS_SECRET_ACCESS_KEY: <redacted>"
