terraform {
  required_providers {
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.3"
    }
  }
}

variable "values_yaml" {
  type = string
}

data "helm_template" "grafana" {
  name  = "grafana-test"
  chart = "${path.module}/marketplace/nebius-grafana/chart"
  values = [
    var.values_yaml,
    yamlencode({ grafana = {
      adminPassword = "fixture-password"
      nebius        = { projectId = "fixture-project", accessToken = "fixture-token" }
    } }),
  ]
  show_only = ["charts/grafana/templates/configSecret.yaml"]
}

locals {
  config_secret = yamldecode(data.helm_template.grafana.manifest)
}

output "datasource_config" {
  value = yamldecode(local.config_secret.stringData["datasources.yaml"])
  precondition {
    condition     = local.config_secret.kind == "Secret"
    error_message = "The chart must render the datasource provisioning file as a Kubernetes Secret."
  }
}
