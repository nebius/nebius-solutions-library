resource "random_password" "grafana_password" {
  count = var.o11y.grafana.enabled ? 1 : 0

  length           = 16
  special          = true
  upper            = true
  lower            = true
  override_special = "@#$%"
}

resource "nebius_applications_v1alpha1_k8s_release" "grafana" {
  count      = var.o11y.grafana.enabled ? 1 : 0
  cluster_id = var.cluster_id
  parent_id  = var.parent_id

  application_name = "grafana-solution-by-nebius"
  namespace        = "o11y"
  product_slug     = "nebius/grafana-solution-by-nebius"

  sensitive = {
    version = nonsensitive(sha256(join(":", [
      var.parent_id,
      var.grafana_observability_key_id,
      random_password.grafana_password[0].result,
    ])))
    set = {
      "grafana.nebius.projectId"   = var.parent_id
      "grafana.adminPassword"      = random_password.grafana_password[0].result
      "grafana.nebius.accessToken" = var.grafana_observability_access_token
    }
  }

  lifecycle {
    precondition {
      condition     = trimspace(var.grafana_observability_access_token) != ""
      error_message = "Grafana requires grafana_observability_access_token. Source environment.sh before planning."
    }
    precondition {
      condition     = trimspace(var.grafana_observability_key_id) != ""
      error_message = "Grafana requires grafana_observability_key_id. Source environment.sh before planning."
    }
  }
}

