mock_provider "nebius" {
  mock_resource "nebius_iam_v2_access_key" {
    defaults = {
      id = "accesskey-e00r7z9vfxmg1bk99s"
    }
  }
}

mock_provider "random" {
  mock_resource "random_password" {
    defaults = {
      result = "mock-grafana-password"
    }
  }
}

variables {
  tenant_id                 = "tenant-e00r7z9vfxmg1bk99s"
  parent_id                 = "project-e00r7z9vfxmg1bk99s"
  cluster_id                = "mk8scluster-e00r7z9vfxmg1bk99s"
  cluster_name              = "k8s-training"
  k8s_node_group_sa_enabled = true
  k8s_node_group_sa_id      = "serviceaccount-e00r7z9vfxmg1bk99s"
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

run "clean_first_apply_reads_generated_token" {
  command = apply

  assert {
    condition     = terraform_data.grafana_access_token[0].output.token_path == "${path.root}/grafana_access_token.txt"
    error_message = "The token path must flow through terraform_data output."
  }

  assert {
    condition     = trimspace(file(terraform_data.grafana_access_token[0].output.token_path)) == "fixture-observability-token-1"
    error_message = "The first apply must generate the token before the ephemeral reader runs."
  }

  assert {
    condition     = jsondecode(file(terraform_data.grafana_access_token[0].output.metadata_path)).id == "static-key-1"
    error_message = "The initial credential metadata must retain its key ID."
  }

  assert {
    condition     = !fileexists(terraform_data.grafana_access_token[0].output.previous_token_path)
    error_message = "A clean first apply must not create a previous credential backup."
  }

  assert {
    condition     = nebius_applications_v1alpha1_k8s_release.grafana[0].sensitive.version == sha256(local.grafana_values)
    error_message = "Grafana's write-only values must have a non-secret version marker."
  }

  assert {
    condition     = yamldecode(local.grafana_values).grafana.deploymentStrategy.type == "Recreate"
    error_message = "Grafana must use Recreate for its ReadWriteOnce volume."
  }

  assert {
    condition     = yamldecode(local.grafana_values).grafana.podAnnotations["nebius.com/observability-token-revision"] == terraform_data.grafana_access_token[0].id
    error_message = "A token replacement must change the Grafana pod template."
  }

  assert {
    condition = length(yamldecode(local.grafana_values).grafana.datasources["datasources.yaml"].datasources) == 4 && alltrue([
      for datasource in yamldecode(local.grafana_values).grafana.datasources["datasources.yaml"].datasources : datasource.version == 0
    ])
    error_message = "All four Nebius datasources must accept repeated provisioning updates."
  }
}

run "forced_replace_rotates_and_preserves_previous" {
  command = apply

  plan_options {
    replace = [
      terraform_data.grafana_access_token[0],
    ]
  }

  assert {
    condition     = trimspace(file(terraform_data.grafana_access_token[0].output.token_path)) == "fixture-observability-token-2"
    error_message = "A forced replacement must install the replacement token."
  }

  assert {
    condition     = trimspace(file(terraform_data.grafana_access_token[0].output.previous_token_path)) == "fixture-observability-token-1"
    error_message = "Rotation must preserve the previous token until validation succeeds."
  }

  assert {
    condition     = jsondecode(file(terraform_data.grafana_access_token[0].output.metadata_path)).id == "static-key-2"
    error_message = "The replacement metadata must contain the new key ID."
  }

  assert {
    condition     = jsondecode(file(terraform_data.grafana_access_token[0].output.previous_metadata_path)).id == "static-key-1"
    error_message = "Rotation must preserve the previous key metadata."
  }

  assert {
    condition     = yamldecode(local.grafana_values).grafana.podAnnotations["nebius.com/observability-token-revision"] == terraform_data.grafana_access_token[0].id
    error_message = "The replacement token resource must update the Grafana pod revision."
  }
}

run "normal_apply_after_rotation_is_idempotent" {
  command = apply

  assert {
    condition     = trimspace(file(terraform_data.grafana_access_token[0].output.token_path)) == "fixture-observability-token-2"
    error_message = "A normal apply after rotation must reuse the active token."
  }

  assert {
    condition     = trimspace(file(terraform_data.grafana_access_token[0].output.previous_token_path)) == "fixture-observability-token-1"
    error_message = "A normal apply must retain the pending previous credential."
  }
}
