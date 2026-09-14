NEBIUS_TENANT_ID=''
NEBIUS_PROJECT_ID=''
NEBIUS_REGION="${NEBIUS_REGION:-eu-west2}"

if [ -z "${NEBIUS_TENANT_ID}" ]; then
  echo "Error: NEBIUS_TENANT_ID is not set"
  return 1
fi

if [ -z "${NEBIUS_PROJECT_ID}" ]; then
  echo "Error: NEBIUS_PROJECT_ID is not set"
  return 1
fi

# region IAM token

unset NEBIUS_IAM_TOKEN
nebius iam whoami > /dev/null
nebius iam get-access-token > /dev/null
NEBIUS_IAM_TOKEN=$(nebius iam get-access-token)
export NEBIUS_IAM_TOKEN

# endregion IAM token

# region VPC subnet

NEBIUS_VPC_SUBNET_ID=$(nebius vpc subnet list \
  --parent-id "${NEBIUS_PROJECT_ID}" \
  --format json \
  | jq -r '.items[0].metadata.id')
export NEBIUS_VPC_SUBNET_ID

# endregion VPC subnet

# region Service account

NEBIUS_SA_TERRAFORM_ID=$(nebius iam service-account list \
  --parent-id "${NEBIUS_PROJECT_ID}" \
  --format json \
  | jq -r '.items[] | select(.metadata.name == "slurm-terraform-sa").metadata.id')

if [ -z "$NEBIUS_SA_TERRAFORM_ID" ]; then
  NEBIUS_SA_TERRAFORM_ID=$(nebius iam service-account create \
    --parent-id "${NEBIUS_PROJECT_ID}" \
    --name 'slurm-terraform-sa' \
    --format json \
    | jq -r '.metadata.id')
  echo "Created new service account with ID: $NEBIUS_SA_TERRAFORM_ID"
else
  echo "Found existing service account with ID: $NEBIUS_SA_TERRAFORM_ID"
fi

# endregion Service account

# region `editors` group

NEBIUS_GROUP_EDITORS_ID=$(nebius iam group get-by-name \
  --parent-id "${NEBIUS_TENANT_ID}" \
  --name 'editors' \
  --format json \
  | jq -r '.metadata.id')

IS_MEMBER=$(nebius iam group-membership list-members \
  --parent-id "${NEBIUS_GROUP_EDITORS_ID}" \
  --page-size 1000 \
  --format json \
  | jq -r --arg SAID "${NEBIUS_SA_TERRAFORM_ID}" '.memberships[] | select(.spec.member_id == $SAID) | .spec.member_id')


# Add service account to group editors only if not already a member
if [ -z "${IS_MEMBER}" ]; then
  nebius iam group-membership create \
    --parent-id "${NEBIUS_GROUP_EDITORS_ID}" \
    --member-id "${NEBIUS_SA_TERRAFORM_ID}"
  echo "Added service account to editors group"
else
  echo "Service account is already a member of editors group"
fi

# endregion `editors` group

# region Access key

DATE_FORMAT='+%Y-%m-%dT%H:%M:%SZ'

if [[ "$(uname)" == "Darwin" ]]; then
  # macOS
  EXPIRATION_DATE=$(date -v +30d "${DATE_FORMAT}")
else
  # Linux (assumes GNU date)
  EXPIRATION_DATE=$(date -d '+30 day' "${DATE_FORMAT}")
fi

echo "Creating new access key for Object Storage expiring at ${EXPIRATION_DATE}"
NEBIUS_SA_ACCESS_KEY_ID=$(nebius iam v2 access-key create \
  --parent-id "${NEBIUS_PROJECT_ID}" \
  --name "s3-access-$(date +%s)" \
  --account-service-account-id "${NEBIUS_SA_TERRAFORM_ID}" \
  --description 'Temporary S3 Access' \
  --expires-at "${EXPIRATION_DATE}" \
  --format json \
  | jq -r '.metadata.id')
echo "Created new access key: ${NEBIUS_SA_ACCESS_KEY_ID}"

# endregion Access key

# region AWS access key

AWS_ACCESS_KEY_ID=$(nebius iam v2 access-key get \
  --id "${NEBIUS_SA_ACCESS_KEY_ID}" \
  --format json | jq -r '.status.aws_access_key_id')
export AWS_ACCESS_KEY_ID

echo "Generating new AWS_SECRET_ACCESS_KEY"
AWS_SECRET_ACCESS_KEY="$(nebius iam v2 access-key get \
  --id "${NEBIUS_SA_ACCESS_KEY_ID}" \
  --format json \
  | jq -r '.status.secret')"
export AWS_SECRET_ACCESS_KEY

# endregion AWS access key
export AWS_ENDPOINT_URL="https://storage.${NEBIUS_REGION}.nebius.cloud:443"
aws configure set aws_access_key_id "${AWS_ACCESS_KEY_ID}"
aws configure set aws_secret_access_key "${AWS_SECRET_ACCESS_KEY}"
aws configure set region "${NEBIUS_REGION}"
aws configure set endpoint_url "${AWS_ENDPOINT_URL}"


echo "If you want to see AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, check out $HOME/.aws/credential."
