mock_provider "nebius" {}
mock_provider "random" {}

variables {
  tenant_id                 = "tenant-e00r7z9vfxmg1bk99s"
  parent_id                 = "project-e00r7z9vfxmg1bk99s"
  cluster_id                = "mk8scluster-e00r7z9vfxmg1bk99s"
  k8s_node_group_sa_id      = "serviceaccount-e00r7z9vfxmg1bk99s"
  k8s_node_group_sa_enabled = true
  o11y                      = { grafana = { enabled = true } }
}

run "deploy_pre_pr_grafana" {
  command   = apply
  state_key = "upgrade"
  module {
    source = "./tests/pre-pr"
  }

  assert {
    condition = (
      trimspace(file("${path.cwd}/grafana_access_token.txt")) == "fixture-observability-token-1" &&
      !fileexists("${path.cwd}/grafana_access_token.metadata.json")
    )
    error_message = "Pre-PR deployments must start with the original token file and no key-ID sidecar."
  }
}

run "upgrade_existing_grafana" {
  command   = apply
  state_key = "upgrade"

  assert {
    condition = (
      terraform_data.grafana_access_token[0].id == run.deploy_pre_pr_grafana.generator_id &&
      nebius_applications_v1alpha1_k8s_release.grafana[0].id == run.deploy_pre_pr_grafana.release_id &&
      nebius_iam_v2_access_key.grafana_key[0].id == run.deploy_pre_pr_grafana.access_key_id
    )
    error_message = "An upgrade must preserve the existing token generator, access key and Grafana release."
  }

  assert {
    condition = (
      trimspace(file(terraform_data.grafana_access_token[0].output.token_path)) == "fixture-observability-token-1" &&
      !fileexists(terraform_data.grafana_access_token[0].output.previous_token_path)
    )
    error_message = "An ordinary upgrade must reuse the pre-PR credential without rotation."
  }

  assert {
    condition = (
      length(module.marketplace_grafana.datasource_config.datasources) == 4 &&
      alltrue([for ds in module.marketplace_grafana.datasource_config.datasources : ds.version == 0])
    )
    error_message = "The upgraded release must provision all four datasources at version 0."
  }
}
