mock_provider "nebius" {}

mock_provider "random" {
  mock_resource "random_password" {
    override_during = plan
    defaults = {
      result = "mock-grafana-password"
    }
  }
}

variables {
  tenant_id                          = "tenant-e00r7z9vfxmg1bk99s"
  parent_id                          = "project-e00r7z9vfxmg1bk99s"
  cluster_id                         = "mk8scluster-e00r7z9vfxmg1bk99s"
  grafana_observability_access_token = "test-observability-token"
  grafana_observability_key_id       = "statickey-test-id"
  o11y = {
    nebius_o11y_agent = { enabled = false }
    prometheus        = { enabled = false }
    loki = {
      enabled = false
      region  = "eu-west2"
    }
    grafana = {
      enabled = true
    }
  }
}

run "first_plan_contains_prepared_token" {
  command = plan

  assert {
    condition = nebius_applications_v1alpha1_k8s_release.grafana[0].sensitive.version == nonsensitive(sha256(join(":", [
      "project-e00r7z9vfxmg1bk99s",
      "statickey-test-id",
      "mock-grafana-password",
    ])))
    error_message = "The static-key ID and admin password must version Grafana's sensitive configuration."
  }
}

run "replacement_key_reaches_grafana" {
  command = plan

  variables {
    grafana_observability_access_token = "replacement-observability-token"
    grafana_observability_key_id       = "statickey-replacement-id"
    o11y = {
      nebius_o11y_agent = { enabled = false }
      prometheus        = { enabled = false }
      loki = {
        enabled = false
        region  = "eu-west2"
      }
      grafana = {
        enabled = true
      }
    }
  }

  assert {
    condition = nebius_applications_v1alpha1_k8s_release.grafana[0].sensitive.version == nonsensitive(sha256(join(":", [
      "project-e00r7z9vfxmg1bk99s",
      "statickey-replacement-id",
      "mock-grafana-password",
    ])))
    error_message = "A replacement static-key ID must update the sensitive configuration version."
  }
}

run "missing_credentials_are_rejected" {
  command = plan

  variables {
    grafana_observability_access_token = ""
    grafana_observability_key_id       = ""
    o11y = {
      nebius_o11y_agent = { enabled = false }
      prometheus        = { enabled = false }
      loki = {
        enabled = false
        region  = "eu-west2"
      }
      grafana = {
        enabled = true
      }
    }
  }

  expect_failures = [
    nebius_applications_v1alpha1_k8s_release.grafana[0],
  ]
}
