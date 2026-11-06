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

unset NEBIUS_IAM_TOKEN
export NEBIUS_IAM_TOKEN=$(nebius iam get-access-token)

export NEBIUS_BUCKET_NAME="tfstate-custom-nat-gateway-$(echo -n "${NEBIUS_TENANT_ID}-${NEBIUS_PROJECT_ID}" | md5sum | awk '$0=$1')"
EXISTS=$(nebius storage bucket list \
  --parent-id "${NEBIUS_PROJECT_ID}" \
  --format json \
  | jq -r --arg BUCKET "${NEBIUS_BUCKET_NAME}" 'try .items[] | select(.metadata.name == $BUCKET) | .metadata.name')

if [ -z "${EXISTS}" ]; then
  nebius storage bucket create \
    --name "${NEBIUS_BUCKET_NAME}" \
    --parent-id "${NEBIUS_PROJECT_ID}" \
    --versioning-policy enabled >/dev/null
  echo "Created bucket: ${NEBIUS_BUCKET_NAME}"
else
  echo "Using existing bucket: ${NEBIUS_BUCKET_NAME}"
fi

cat > terraform_backend_override.tf <<EOF
terraform {
  backend "s3" {
    bucket = "${NEBIUS_BUCKET_NAME}"
    key    = "custom-nat-gateway.tfstate"

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

export TF_VAR_parent_id="${NEBIUS_PROJECT_ID}"

echo "Exported variables:"
echo "NEBIUS_TENANT_ID: ${NEBIUS_TENANT_ID}"
echo "NEBIUS_PROJECT_ID: ${NEBIUS_PROJECT_ID}"
echo "NEBIUS_REGION: ${NEBIUS_REGION}"
echo "NEBIUS_BUCKET_NAME: ${NEBIUS_BUCKET_NAME}"
