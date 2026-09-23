#!/bin/bash

# Edit these values for your environment before sourcing this file.
NEBIUS_TENANT_ID='tenant-xxxxxxxxxxxxxxxxxxxx'
NEBIUS_PROJECT_ID_REGION1='project-xxxxxxxxxxxxxxxxxxxx'
NEBIUS_PROJECT_ID_REGION2='project-yyyyyyyyyyyyyyyyyyyy'

NEBIUS_REGION1='eu-north1'
NEBIUS_REGION2='eu-west1'

if [ -z "${NEBIUS_TENANT_ID}" ]; then
  echo "Error: NEBIUS_TENANT_ID is not set"
  return 1
fi

if [ -z "${NEBIUS_PROJECT_ID_REGION1}" ] || [ -z "${NEBIUS_PROJECT_ID_REGION2}" ]; then
  echo "Error: NEBIUS_PROJECT_ID_REGION1 and NEBIUS_PROJECT_ID_REGION2 are required"
  return 1
fi

if [ -z "${NEBIUS_REGION1}" ] || [ -z "${NEBIUS_REGION2}" ]; then
  echo "Error: NEBIUS_REGION1 and NEBIUS_REGION2 are required"
  return 1
fi

# IAM token
unset NEBIUS_IAM_TOKEN
export NEBIUS_IAM_TOKEN=$(nebius iam get-access-token)

NEBIUS_VPC_SUBNET_ID_REGION1=$(nebius vpc subnet list \
  --parent-id "${NEBIUS_PROJECT_ID_REGION1}" \
  --format json \
  | jq -r '.items[0].metadata.id')
NEBIUS_VPC_SUBNET_ID_REGION2=$(nebius vpc subnet list \
  --parent-id "${NEBIUS_PROJECT_ID_REGION2}" \
  --format json \
  | jq -r '.items[0].metadata.id')

if [ -z "${NEBIUS_VPC_SUBNET_ID_REGION1}" ] || [ -z "${NEBIUS_VPC_SUBNET_ID_REGION2}" ]; then
  echo "Error: failed to resolve subnet IDs for regions ${NEBIUS_REGION1} and/or ${NEBIUS_REGION2}"
  return 1
fi

export NEBIUS_VPC_SUBNET_ID_REGION1
export NEBIUS_VPC_SUBNET_ID_REGION2

# Object Storage Bucket
export NEBIUS_BUCKET_NAME="skypilot-multiregion$(echo -n "${NEBIUS_TENANT_ID}-${NEBIUS_PROJECT_ID_REGION1}" | md5sum | awk '$0=$1')"
BUCKET_LIST_JSON=$(nebius storage bucket list \
  --parent-id "${NEBIUS_PROJECT_ID_REGION1}" \
  --format json)
EXISTS=$(jq -r --arg BUCKET "${NEBIUS_BUCKET_NAME}" 'try .items[] | select(.metadata.name == $BUCKET) | .metadata.name' <<<"${BUCKET_LIST_JSON}")
if [ -z "${EXISTS}" ]; then
  RESPONSE=$(nebius storage bucket create \
    --name "${NEBIUS_BUCKET_NAME}" \
    --parent-id "${NEBIUS_PROJECT_ID_REGION1}" \
    --versioning-policy 'enabled')
  echo "Created bucket: ${NEBIUS_BUCKET_NAME}"
  BUCKET_LIST_JSON=$(nebius storage bucket list \
    --parent-id "${NEBIUS_PROJECT_ID_REGION1}" \
    --format json)
else
  echo "Using existing bucket: ${NEBIUS_BUCKET_NAME}"
fi

NEBIUS_BUCKET_REGION=$(jq -r --arg BUCKET "${NEBIUS_BUCKET_NAME}" '.items[] | select(.metadata.name == $BUCKET) | .status.region' <<<"${BUCKET_LIST_JSON}")
if [ -z "${NEBIUS_BUCKET_REGION}" ] || [ "${NEBIUS_BUCKET_REGION}" = "null" ]; then
  NEBIUS_BUCKET_REGION="${NEBIUS_REGION1}"
fi
export NEBIUS_BUCKET_REGION

# Nebius service account
NEBIUS_SA_NAME="infra-sa"
NEBIUS_SA_ID=$(nebius iam service-account get-by-name \
  --parent-id "${NEBIUS_PROJECT_ID_REGION1}" \
  --name "${NEBIUS_SA_NAME}" \
  --format json \
  | jq -r '.metadata.id')


if [ -z "$NEBIUS_SA_ID" ]; then
  NEBIUS_SA_ID=$(nebius iam service-account create \
    --parent-id "${NEBIUS_PROJECT_ID_REGION1}" \
    --name "${NEBIUS_SA_NAME}" \
    --format json \
    | jq -r '.metadata.id')
  echo "Created new service account: $NEBIUS_SA_ID"
else
  echo "Using existing service account: $NEBIUS_SA_ID"
fi

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
  --parent-id "${NEBIUS_PROJECT_ID_REGION1}" \
  --name "infra-tfstate-$(date +%s)" \
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

# Use Object Storage as Terraform backend
cat > terraform_backend_override.tf << EOF
terraform {
  backend "s3" {
    bucket = "${NEBIUS_BUCKET_NAME}"
    key    = "skypilot-multiregion-tfstate"

    endpoints = {
      s3 = "https://storage.${NEBIUS_BUCKET_REGION}.nebius.cloud:443"
    }
    region = "${NEBIUS_BUCKET_REGION}"

    skip_region_validation      = true
    skip_credentials_validation = true
    skip_requesting_account_id  = true
    skip_s3_checksum            = true
  }
}
EOF

# Terraform variables
export TF_VAR_iam_token="${NEBIUS_IAM_TOKEN}"
export TF_VAR_parent_id_region1="${NEBIUS_PROJECT_ID_REGION1}"
export TF_VAR_parent_id_region2="${NEBIUS_PROJECT_ID_REGION2}"
export TF_VAR_region1="${NEBIUS_REGION1}"
export TF_VAR_region2="${NEBIUS_REGION2}"
export TF_VAR_subnet_id_region1="${NEBIUS_VPC_SUBNET_ID_REGION1}"
export TF_VAR_subnet_id_region2="${NEBIUS_VPC_SUBNET_ID_REGION2}"
export TF_VAR_tenant_id="${NEBIUS_TENANT_ID}"

# Also persist values for users who run `./environment.sh` (non-sourced shell).
cat > zzz_environment.auto.tfvars << EOF
parent_id_region1   = "${NEBIUS_PROJECT_ID_REGION1}"
parent_id_region2   = "${NEBIUS_PROJECT_ID_REGION2}"
region1            = "${NEBIUS_REGION1}"
region2            = "${NEBIUS_REGION2}"
subnet_id_region1  = "${NEBIUS_VPC_SUBNET_ID_REGION1}"
subnet_id_region2  = "${NEBIUS_VPC_SUBNET_ID_REGION2}"
EOF

# Exported variables
echo "Exported variables:"
echo "NEBIUS_TENANT_ID: ${NEBIUS_TENANT_ID}"
echo "NEBIUS_PROJECT_ID_REGION1: ${NEBIUS_PROJECT_ID_REGION1}"
echo "NEBIUS_PROJECT_ID_REGION2: ${NEBIUS_PROJECT_ID_REGION2}"
echo "NEBIUS_REGION1: ${NEBIUS_REGION1}"
echo "NEBIUS_REGION2: ${NEBIUS_REGION2}"
echo "NEBIUS_VPC_SUBNET_ID_REGION1: ${NEBIUS_VPC_SUBNET_ID_REGION1}"
echo "NEBIUS_VPC_SUBNET_ID_REGION2: ${NEBIUS_VPC_SUBNET_ID_REGION2}"
echo "Wrote: zzz_environment.auto.tfvars"
echo "NEBIUS_BUCKET_NAME: ${NEBIUS_BUCKET_NAME}"
echo "NEBIUS_BUCKET_REGION: ${NEBIUS_BUCKET_REGION}"
echo "AWS_ACCESS_KEY_ID: ${AWS_ACCESS_KEY_ID}"
echo "AWS_SECRET_ACCESS_KEY: <redacted>"
