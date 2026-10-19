#!/bin/bash

# Set the following environment variables:
# NEBIUS_TENANT_ID='tenant-...'
# NEBIUS_PROJECT_ID='project-...'
# NEBIUS_REGION='eu-west2'

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

# VPC subnet
NEBIUS_VPC_SUBNET_ID=$(nebius vpc subnet list \
  --parent-id "${NEBIUS_PROJECT_ID}" \
  --format json \
  | jq -r '.items[0].metadata.id')
export NEBIUS_VPC_SUBNET_ID

# Object Storage Bucket
if command -v md5sum >/dev/null 2>&1; then
  BUCKET_HASH=$(echo -n "${NEBIUS_TENANT_ID}-${NEBIUS_PROJECT_ID}" | md5sum | awk '$0=$1')
else
  # BSD md5 (stock macOS) — same digest, -q prints the hash only, so the
  # bucket name is identical across Linux and macOS.
  BUCKET_HASH=$(echo -n "${NEBIUS_TENANT_ID}-${NEBIUS_PROJECT_ID}" | md5 -q)
fi
export NEBIUS_BUCKET_NAME="tfstate-k8s-training-${BUCKET_HASH}"
EXISTS=$(nebius storage bucket list \
  --parent-id "${NEBIUS_PROJECT_ID}" \
  --format json \
  | jq -r --arg BUCKET "${NEBIUS_BUCKET_NAME}" 'try .items[] | select(.metadata.name == $BUCKET) | .metadata.name')
if [ -z "${EXISTS}" ]; then
  RESPONSE=$(nebius storage bucket create \
    --name "${NEBIUS_BUCKET_NAME}" \
    --parent-id "${NEBIUS_PROJECT_ID}" \
    --versioning-policy 'enabled')
  echo "Created bucket: ${NEBIUS_BUCKET_NAME}"
else
  echo "Using existing bucket: ${NEBIUS_BUCKET_NAME}"
fi

# Resolve the bucket's S3 endpoint from the API (status.domain_name) rather
# than hand-building storage.<region>.nebius.cloud — the API value is
# authoritative if a region's endpoint ever differs from that pattern.
BUCKET_INFO_OUTPUT=$(nebius storage bucket list \
  --parent-id "${NEBIUS_PROJECT_ID}" \
  --format json) || { echo "Error: failed to list storage buckets" >&2; return 1; }
NEBIUS_BUCKET_DOMAIN=$(printf '%s' "${BUCKET_INFO_OUTPUT}" | jq -r --arg BUCKET "${NEBIUS_BUCKET_NAME}" '(.items[] | select(.metadata.name == $BUCKET) | .status.domain_name) // empty')
if [ -z "${NEBIUS_BUCKET_DOMAIN}" ] || [ "${NEBIUS_BUCKET_DOMAIN}" = "null" ]; then
  NEBIUS_BUCKET_DOMAIN="storage.${NEBIUS_REGION}.nebius.cloud"
  echo "Warning: bucket domain_name not reported by the API; falling back to ${NEBIUS_BUCKET_DOMAIN}" >&2
fi
export NEBIUS_BUCKET_DOMAIN

# Nebius service account
NEBIUS_SA_NAME="k8s-training-sa"

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

# Ensure service account is member of editors group
NEBIUS_GROUP_EDITORS_ID=$(nebius iam group get-by-name \
  --parent-id "${NEBIUS_TENANT_ID}" \
  --name 'editors' \
  --format json \
  | jq -r '.metadata.id')
IS_MEMBER=$(nebius iam group-membership list-members \
  --parent-id "${NEBIUS_GROUP_EDITORS_ID}" \
  --page-size 1000 \
  --format json \
  | jq -r --arg SAID "${NEBIUS_SA_ID}" '.memberships[] | select(.spec.member_id == $SAID) | .spec.member_id')
if [ -z "${IS_MEMBER}" ]; then
  RESPONSE=$(nebius iam group-membership create \
    --parent-id "${NEBIUS_GROUP_EDITORS_ID}" \
    --member-id "${NEBIUS_SA_ID}")
  echo "Added service account to editors group"
else
  echo "Service account is already a member of editors group"
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
  --name "k8s-training-tfstate-$(date +%s)" \
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

# Use Objext Storage as Terraform backend
cat > terraform_backend_override.tf << EOF
terraform {
  backend "s3" {
    bucket = "${NEBIUS_BUCKET_NAME}"
    key    = "k8s-training.tfstate"

    endpoints = {
      s3 = "https://${NEBIUS_BUCKET_DOMAIN}:443"
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
echo "NEBIUS_BUCKET_DOMAIN: ${NEBIUS_BUCKET_DOMAIN}"
echo "AWS_ACCESS_KEY_ID: ${AWS_ACCESS_KEY_ID}"
echo "AWS_SECRET_ACCESS_KEY: <redacted>"
