resource "random_password" "grafana_password" {
  count = var.o11y.grafana.enabled ? 1 : 0

  length           = 16
  special          = true
  upper            = true
  lower            = true
  override_special = "@#$%"
}

# Static access key for the existing k8s_node_group_sa service account
resource "nebius_iam_v2_access_key" "grafana_key" {
  count       = var.o11y.grafana.enabled && var.k8s_node_group_sa_enabled ? 1 : 0
  parent_id   = var.parent_id
  name        = "${var.cluster_name}.grafana-access-key"
  description = "Access key for Grafana module"
  account = {
    service_account = {
      id = var.k8s_node_group_sa_id
    }
  }
}

# Generate access token from static key
resource "terraform_data" "grafana_access_token" {
  count = var.o11y.grafana.enabled && var.k8s_node_group_sa_enabled ? 1 : 0

  triggers_replace = {
    access_key_id      = nebius_iam_v2_access_key.grafana_key[0].id
    service_account_id = var.k8s_node_group_sa_id
  }

  provisioner "local-exec" {
    when        = create
    working_dir = path.root
    interpreter = ["/bin/bash", "-c"]
    command     = <<EOT
set -e

# Get the access key details
ACCESS_KEY_ID="${self.triggers_replace.access_key_id}"
SERVICE_ACCOUNT_ID="${self.triggers_replace.service_account_id}"

# Generate access token using the static key
echo "Generating access token for service account: $SERVICE_ACCOUNT_ID"
TOKEN=$(nebius iam static-key issue \
  --parent-id "${var.parent_id}" \
  --account-service-account-id "$SERVICE_ACCOUNT_ID" \
  --service observability \
  --name "grafana-token-$(date +%s)" \
  --format json | jq -r '.token')

if [ -z "$TOKEN" ] || [ "$TOKEN" = "null" ]; then
  echo "Failed to generate access token"
  exit 1
fi

echo "Access token generated successfully"
echo "$TOKEN" > "${path.root}/grafana_access_token.txt"
EOT
  }

  provisioner "local-exec" {
    when        = destroy
    working_dir = path.root
    interpreter = ["/bin/bash", "-c"]
    command     = <<EOT
# Clean up the token file
rm -f "${path.root}/grafana_access_token.txt"
EOT
  }
}

locals {
  grafana_access_token_path = "${path.root}/grafana_access_token.txt"
  grafana_access_token      = var.o11y.grafana.enabled && var.k8s_node_group_sa_enabled ? try(trimspace(file(local.grafana_access_token_path)), "") : ""
}

resource "nebius_applications_v1alpha1_k8s_release" "grafana" {
  count      = var.o11y.grafana.enabled ? 1 : 0
  cluster_id = var.cluster_id
  parent_id  = var.parent_id

  application_name = "grafana-solution-by-nebius"
  namespace        = "o11y"
  product_slug     = "nebius/grafana-solution-by-nebius"

  sensitive = {
    set = {
      "grafana.nebius.projectId"   = var.parent_id
      "grafana.adminPassword"      = random_password.grafana_password[0].result
      "grafana.nebius.accessToken" = local.grafana_access_token
    }
  }
}

