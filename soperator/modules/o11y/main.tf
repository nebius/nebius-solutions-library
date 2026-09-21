resource "terraform_data" "o11y_static_key_secret" {
  triggers_replace = {
    o11y_resources_name              = local.o11y_resources_name
    k8s_cluster_context              = var.k8s_cluster_context
    o11y_iam_tenant_id               = var.o11y_iam_tenant_id
    o11y_secret_name                 = var.o11y_secret_name
    o11y_secret_logs_namespace       = var.o11y_secret_logs_namespace
    o11y_secret_monitoring_namespace = var.o11y_secret_monitoring_namespace
    o11y_profile                     = var.o11y_profile
    iam_project_id                   = var.iam_project_id
    company_name                     = var.company_name
  }

  provisioner "local-exec" {
    when        = create
    working_dir = path.root
    interpreter = ["/bin/bash", "-c"]
    command     = <<EOT
set -e

NEBIUS_IAM_TOKEN_BKP=$NEBIUS_IAM_TOKEN
unset NEBIUS_IAM_TOKEN

# Ensuring that profile exists
if ! nebius profile list | grep -Fxq ${self.triggers_replace.o11y_profile}; then
  CURRENT_PROFILE=$(nebius profile current)
  nebius profile create --endpoint api.eu.nebius.cloud --federation-endpoint auth.eu.nebius.com --parent-id ${self.triggers_replace.o11y_iam_tenant_id} ${self.triggers_replace.o11y_profile}
  nebius profile activate $CURRENT_PROFILE
fi
export NEBIUS_IAM_TOKEN=$(nebius --profile ${self.triggers_replace.o11y_profile} iam get-access-token)

# Creating new project for cluster logs
echo "Creating new project for cluster logs..."
nebius iam project create --parent-id ${self.triggers_replace.o11y_iam_tenant_id} --name ${self.triggers_replace.o11y_resources_name} --labels original-project-id=${self.triggers_replace.iam_project_id},company-name=${self.triggers_replace.company_name} || true
output=$(nebius iam project get-by-name --parent-id ${self.triggers_replace.o11y_iam_tenant_id} --name ${self.triggers_replace.o11y_resources_name} --format json)
status=$?
if [ $status -ne 0 ]; then
    echo "Failed to get project"
    exit 1
fi

PROJECT_ID=$(echo "$output" | jq -r .metadata.id)
echo "Project for logs $PROJECT_ID"

# Creating group, service account, group-membership and access-permit.
echo "Creating service account..."
nebius iam service-account create --name "${self.triggers_replace.o11y_resources_name}" --parent-id $PROJECT_ID || true
output=$(nebius iam service-account get-by-name --name "${self.triggers_replace.o11y_resources_name}" --parent-id $PROJECT_ID --format json)
status=$?
if [ $status -ne 0 ]; then
    echo "Failed to get service account"
    exit 1
fi

SA=$(echo "$output" | jq -r .metadata.id)
echo "Service account for logs: $SA"

echo "Creating group..."
nebius iam group create --name "${self.triggers_replace.o11y_resources_name}" --parent-id ${self.triggers_replace.o11y_iam_tenant_id} || true
output=$(nebius iam group get-by-name --name "${self.triggers_replace.o11y_resources_name}" --parent-id ${self.triggers_replace.o11y_iam_tenant_id} --format json)
status=$?
if [ $status -ne 0 ]; then
    echo "Failed to get group"
    exit 1
fi

GROUP=$(echo "$output" | jq -r .metadata.id)

echo "Adding service account to the iam group $GROUP..."
nebius iam group-membership create --member-id $SA --parent-id "$GROUP" || true
IS_MEMBER=$(nebius iam group-membership list-members --parent-id "$GROUP" --page-size 1000 --format json | jq -r --arg SA $SA '.memberships[] | select(.spec.member_id == $SA) | .spec.member_id')
if [ -z "$IS_MEMBER" ] || [ "$IS_MEMBER" == "null" ]; then
  echo "Group-membership is not created"
  exit 1
fi

echo "Service account was successfully added to the iam group."

echo "Creating access-permit..."
nebius iam access-permit create --parent-id "$GROUP" --role logging.logs.writer --resource-id "$PROJECT_ID"

# Issuing static key and creating k8s secret
echo "Deleting previous static key if exists..."
STATIC_KEY=$(nebius iam static-key get-by-name --parent-id "$PROJECT_ID" --name ${self.triggers_replace.o11y_resources_name} --format json | jq -r .metadata.id)
if [ ! -z "$STATIC_KEY" ] && [ "$STATIC_KEY" != "null" ]; then
  echo "Deleting $STATIC_KEY..."
  nebius iam static-key delete --id "$STATIC_KEY"
fi

echo "Issuing new static key..."
output=$(nebius iam static-key issue --parent-id "$PROJECT_ID" \
  --account-service-account-id "$SA" \
  --service observability \
  --name ${self.triggers_replace.o11y_resources_name} \
  --format json)
status=$?
if [ $status -ne 0 ]; then
    echo "Failed to issue static key"
    exit 1
fi

TOKEN=$(echo $output | jq -r .token)


export NEBIUS_IAM_TOKEN=$NEBIUS_IAM_TOKEN_BKP

echo "Applying namespace..."
cat <<EOF | "${path.module}/../scripts/kubectl_apply_with_retries.sh" --context "${self.triggers_replace.k8s_cluster_context}"
apiVersion: v1
kind: Namespace
metadata:
  name: ${self.triggers_replace.o11y_secret_logs_namespace}
EOF
cat <<EOF | "${path.module}/../scripts/kubectl_apply_with_retries.sh" --context "${self.triggers_replace.k8s_cluster_context}"
apiVersion: v1
kind: Namespace
metadata:
  name: ${self.triggers_replace.o11y_secret_monitoring_namespace}
EOF

echo "Creating secret..."
kubectl --context "${self.triggers_replace.k8s_cluster_context}" create secret generic "${self.triggers_replace.o11y_secret_name}" \
  -n ${self.triggers_replace.o11y_secret_logs_namespace} \
  --from-literal=accessToken="$TOKEN" \
  --dry-run=client \
  -o yaml \
  | "${path.module}/../scripts/kubectl_apply_with_retries.sh" --server-side --context "${self.triggers_replace.k8s_cluster_context}"
EOT
  }

  provisioner "local-exec" {
    when        = destroy
    working_dir = path.root
    interpreter = ["/bin/bash", "-c"]
    command     = <<EOT
set -e

unset NEBIUS_IAM_TOKEN
export NEBIUS_IAM_TOKEN=$(nebius --profile ${self.triggers_replace.o11y_profile} iam get-access-token)

output=$(nebius iam project get-by-name --name "${self.triggers_replace.o11y_resources_name}" --parent-id "${self.triggers_replace.o11y_iam_tenant_id}" --format json)
status=$?
if [ $status -ne 0 ]; then
    echo "Failed to get project"
    exit 1
fi

PROJECT_ID=$(echo $output |  jq -r .metadata.id)

echo "Deleting service account..."
SA=$(nebius iam service-account get-by-name --name "${self.triggers_replace.o11y_resources_name}" --parent-id $PROJECT_ID --format json | jq -r .metadata.id)
if [ ! -z "$SA" ] && [ "$SA" != "null" ]; then
  nebius iam service-account delete --id "$SA"
fi

GROUP=$(nebius iam group get-by-name --name "${self.triggers_replace.o11y_resources_name}" --parent-id "${self.triggers_replace.o11y_iam_tenant_id}" --format json | jq -r .metadata.id)
if [ ! -z "$GROUP" ] && [ "$GROUP" != "null" ]; then
  nebius iam group delete --id "$GROUP"
fi

EOT
  }
}

resource "terraform_data" "opentelemetry_collector_cm" {
  depends_on = [
    terraform_data.o11y_static_key_secret
  ]

  triggers_replace = {
    k8s_cluster_context = var.k8s_cluster_context
    o11y_resources_name = local.o11y_resources_name
    o11y_iam_tenant_id  = var.o11y_iam_tenant_id
    o11y_profile        = var.o11y_profile
    configmap_name      = var.opentelemetry_collector_cm
    iam_project_id      = var.iam_project_id
  }

  provisioner "local-exec" {
    when        = create
    working_dir = path.root
    interpreter = ["/bin/bash", "-c"]
    command     = <<EOT
set -e

NEBIUS_IAM_TOKEN_BKP=$NEBIUS_IAM_TOKEN
unset NEBIUS_IAM_TOKEN
export NEBIUS_IAM_TOKEN=$(nebius --profile ${self.triggers_replace.o11y_profile} iam get-access-token)

PROJECT_ID=$(nebius iam project get-by-name --parent-id ${self.triggers_replace.o11y_iam_tenant_id} --name ${self.triggers_replace.o11y_resources_name} --format json | jq -r .metadata.id)
O11YWORKSPACE_ID=$(echo "$PROJECT_ID" | sed 's#project-#o11yworkspace-#')

export NEBIUS_IAM_TOKEN=$NEBIUS_IAM_TOKEN_BKP

echo "Applying opentelemetry controller configmap with $PROJECT_ID..."
cat <<EOF | "${path.module}/../scripts/kubectl_apply_with_retries.sh" --context "${self.triggers_replace.k8s_cluster_context}"
apiVersion: v1
kind: ConfigMap
metadata:
  name: ${self.triggers_replace.configmap_name}
  namespace: flux-system
data:
  values.yaml: |
    observability:
      logsProjectId: $PROJECT_ID
      metricsProjectId: ${self.triggers_replace.iam_project_id}
    soperatorActiveChecks:
      overrideValues:
        checks:
          extensive-check:
            slurmJobSpec:
              jobContainer:
                extraEnv:
                  - name: "SLURM_EXTRA_COMMENT_JSON"
                    value: "{\"o11y_workspace\": \"$O11YWORKSPACE_ID\"}"
EOF
EOT
  }
}
