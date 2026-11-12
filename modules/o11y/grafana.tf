resource "random_password" "grafana_password" {
  count = var.o11y.grafana.enabled ? 1 : 0

  length           = 16
  special          = true
  upper            = true
  lower            = true
  override_special = "@#$%"
}

locals {
  grafana_access_token_enabled                = var.o11y.grafana.enabled && var.k8s_node_group_sa_enabled
  grafana_access_token_path                   = "${path.root}/grafana_access_token.txt"
  grafana_access_token_metadata_path          = "${path.root}/grafana_access_token.metadata.json"
  grafana_previous_access_token_path          = "${path.root}/grafana_access_token.previous.txt"
  grafana_previous_access_token_metadata_path = "${path.root}/grafana_access_token.previous.metadata.json"
}

# Static access key for the existing k8s_node_group_sa service account
resource "nebius_iam_v2_access_key" "grafana_key" {
  count       = var.o11y.grafana.enabled && var.k8s_node_group_sa_enabled ? 1 : 0
  parent_id   = var.parent_id
  name        = var.cluster_name == null ? "grafana-access-key" : "${var.cluster_name}.grafana-access-key"
  description = "Access key for Grafana module"
  account = {
    service_account = {
      id = var.k8s_node_group_sa_id
    }
  }
}

# Generate access token from static key
resource "terraform_data" "grafana_access_token" {
  count = local.grafana_access_token_enabled ? 1 : 0

  # Expose the paths as an apply-time output. The ephemeral reader below uses
  # this output so Terraform waits for token generation before reading it.
  input = {
    token_path             = local.grafana_access_token_path
    metadata_path          = local.grafana_access_token_metadata_path
    previous_token_path    = local.grafana_previous_access_token_path
    previous_metadata_path = local.grafana_previous_access_token_metadata_path
  }

  # Intentional rotation uses Terraform's one-shot -replace option. Keeping the
  # stable dependency map here prevents normal applies from rotating the key.
  triggers_replace = {
    access_key_id      = nebius_iam_v2_access_key.grafana_key[0].id
    service_account_id = var.k8s_node_group_sa_id
  }

  lifecycle {
    create_before_destroy = true
  }

  provisioner "local-exec" {
    when        = create
    working_dir = path.root
    interpreter = ["/bin/bash", "-c"]
    environment = {
      CURRENT_TOKEN_FILE     = self.input.token_path
      CURRENT_METADATA_FILE  = self.input.metadata_path
      PREVIOUS_TOKEN_FILE    = self.input.previous_token_path
      PREVIOUS_METADATA_FILE = self.input.previous_metadata_path
      PARENT_ID              = var.parent_id
      SCRIPT_PATH            = "${path.module}/scripts/grafana-observability-key.sh"
      SERVICE_ACCOUNT_ID     = self.triggers_replace.service_account_id
    }
    command = "/bin/bash \"$SCRIPT_PATH\" issue"
  }
}

# Keep local credentials across generator replacement, but remove them when
# Grafana is disabled or the enclosing Terraform configuration is destroyed.
resource "terraform_data" "grafana_access_token_files" {
  count = local.grafana_access_token_enabled ? 1 : 0

  input = {
    token_path             = local.grafana_access_token_path
    metadata_path          = local.grafana_access_token_metadata_path
    previous_token_path    = local.grafana_previous_access_token_path
    previous_metadata_path = local.grafana_previous_access_token_metadata_path
  }

  depends_on = [terraform_data.grafana_access_token]

  provisioner "local-exec" {
    when        = destroy
    working_dir = path.root
    interpreter = ["/bin/bash", "-c"]
    environment = {
      CURRENT_TOKEN_FILE     = self.output.token_path
      CURRENT_METADATA_FILE  = self.output.metadata_path
      PREVIOUS_TOKEN_FILE    = self.output.previous_token_path
      PREVIOUS_METADATA_FILE = self.output.previous_metadata_path
      SCRIPT_PATH            = "${path.module}/scripts/grafana-observability-key.sh"
    }
    command = "/bin/bash \"$SCRIPT_PATH\" cleanup"
  }
}

# Read the token only after terraform_data has finished creating it. stdout is
# ephemeral and can only flow into another ephemeral or write-only attribute.
ephemeral "local_command" "grafana_access_token" {
  count = local.grafana_access_token_enabled ? 1 : 0

  command = "/bin/bash"
  arguments = [
    "-c",
    "set -euo pipefail; test -s \"$1\" || { echo \"Grafana Observability token file is missing or empty: $1\" >&2; exit 1; }; chmod 600 \"$1\"; cat -- \"$1\"",
    "grafana-token-reader",
    terraform_data.grafana_access_token[0].output.token_path,
  ]
}

locals {
  grafana_token_revision = local.grafana_access_token_enabled ? terraform_data.grafana_access_token[0].id : "disabled"
  grafana_values = yamlencode({
    grafana = {
      deploymentStrategy = {
        type = "Recreate"
      }
      podAnnotations = {
        "nebius.com/observability-token-revision" = local.grafana_token_revision
      }
      # Helm replaces datasource lists rather than merging individual entries.
      # Keep this complete list synchronized with nebius-grafana/chart/values.yaml.
      datasources = {
        "datasources.yaml" = {
          apiVersion = 1
          datasources = [
            {
              name      = "Nebius Services"
              type      = "prometheus"
              access    = "proxy"
              url       = "https://read.monitoring.api.nebius.cloud/projects/{{ .Values.nebius.projectId }}/service-provider/prometheus"
              isDefault = true
              jsonData = {
                httpHeaderName1 = "Authorization"
              }
              secureJsonData = {
                httpHeaderValue1 = "Bearer {{ .Values.nebius.accessToken }}"
              }
              version = 0
            },
            {
              name      = "Nebius Monitoring"
              type      = "prometheus"
              access    = "proxy"
              url       = "https://read.monitoring.api.nebius.cloud/projects/{{ .Values.nebius.projectId }}/prometheus"
              isDefault = false
              jsonData = {
                httpHeaderName1 = "Authorization"
              }
              secureJsonData = {
                httpHeaderValue1 = "Bearer {{ .Values.nebius.accessToken }}"
              }
              version = 0
            },
            {
              name      = "Nebius Logging"
              type      = "loki"
              access    = "proxy"
              url       = "https://read.logging.api.nebius.cloud/projects/{{ .Values.nebius.projectId }}"
              isDefault = false
              jsonData = {
                httpHeaderName1 = "Authorization"
              }
              secureJsonData = {
                httpHeaderValue1 = "Bearer {{ .Values.nebius.accessToken }}"
              }
              version = 0
            },
            {
              name      = "Nebius Tracing"
              type      = "tempo"
              access    = "proxy"
              url       = "https://read.tracing.api.nebius.cloud/projects/{{ .Values.nebius.projectId }}/tempo"
              isDefault = false
              jsonData = {
                httpHeaderName1 = "Authorization"
              }
              secureJsonData = {
                httpHeaderValue1 = "Bearer {{ .Values.nebius.accessToken }}"
              }
              version = 0
            },
          ]
        }
      }
    }
  })
}

resource "nebius_applications_v1alpha1_k8s_release" "grafana" {
  count      = var.o11y.grafana.enabled ? 1 : 0
  cluster_id = var.cluster_id
  parent_id  = var.parent_id

  application_name = "grafana-solution-by-nebius"
  namespace        = "o11y"
  product_slug     = "nebius/grafana-solution-by-nebius"

  sensitive = {
    # The values are write-only. Their non-secret hash is retained in state so
    # a changed token revision causes the provider to resend them.
    version = sha256(local.grafana_values)
    values  = local.grafana_values
    set = {
      "grafana.nebius.projectId"   = var.parent_id
      "grafana.adminPassword"      = random_password.grafana_password[0].result
      "grafana.nebius.accessToken" = local.grafana_access_token_enabled ? trimspace(ephemeral.local_command.grafana_access_token[0].stdout) : ""
    }
  }
}

