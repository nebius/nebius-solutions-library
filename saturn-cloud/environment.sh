#!/bin/bash

# Set the following environment variables:
# NEBIUS_TENANT_ID='tenant-...'
# NEBIUS_PROJECT_ID='project-...'
# NEBIUS_REGION='eu-north1'

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

export SATURN_DEPLOYMENT_NAME="${SATURN_DEPLOYMENT_NAME:-saturn-cloud}"

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
export NEBIUS_BUCKET_NAME="tfstate-saturn-cloud-$(echo -n "${NEBIUS_TENANT_ID}-${NEBIUS_PROJECT_ID}" | md5sum | awk '$0=$1')"
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

# Nebius service account
NEBIUS_SA_NAME="${SATURN_DEPLOYMENT_NAME}-sa"
NEBIUS_SA_ID=$(nebius iam service-account get-by-name \
  --parent-id "${NEBIUS_PROJECT_ID}" \
  --name "${NEBIUS_SA_NAME}" \
  --format json \
  | jq -r '.metadata.id')

if [ -z "$NEBIUS_SA_ID" ]; then
  NEBIUS_SA_ID=$(nebius iam service-account create \
    --parent-id "${NEBIUS_PROJECT_ID}" \
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
  --parent-id "${NEBIUS_PROJECT_ID}" \
  --name "saturn-cloud-tfstate-$(date +%s)" \
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
    key    = "${SATURN_DEPLOYMENT_NAME}.tfstate"

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

# Viewers group ID (for container registry access)
NEBIUS_VIEWERS_GROUP_ID=$(nebius iam group get-by-name \
  --parent-id "${NEBIUS_TENANT_ID}" \
  --name 'viewers' \
  --format json \
  | jq -r '.metadata.id')
export NEBIUS_VIEWERS_GROUP_ID

# Terraform variables
export TF_VAR_iam_token="${NEBIUS_IAM_TOKEN}"
export TF_VAR_project_id="${NEBIUS_PROJECT_ID}"
export TF_VAR_region="${NEBIUS_REGION}"
export TF_VAR_subnet_id="${NEBIUS_VPC_SUBNET_ID}"
export TF_VAR_viewers_group_id="${NEBIUS_VIEWERS_GROUP_ID}"

# Exported variables
echo "Exported variables:"
echo "NEBIUS_TENANT_ID: ${NEBIUS_TENANT_ID}"
echo "NEBIUS_PROJECT_ID: ${NEBIUS_PROJECT_ID}"
echo "NEBIUS_REGION: ${NEBIUS_REGION}"
echo "NEBIUS_VPC_SUBNET_ID: ${NEBIUS_VPC_SUBNET_ID}"
echo "NEBIUS_BUCKET_NAME: ${NEBIUS_BUCKET_NAME}"
echo "NEBIUS_VIEWERS_GROUP_ID: ${NEBIUS_VIEWERS_GROUP_ID}"
echo "AWS_ACCESS_KEY_ID: ${AWS_ACCESS_KEY_ID}"
echo "AWS_SECRET_ACCESS_KEY: <redacted>"
