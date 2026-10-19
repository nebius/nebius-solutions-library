# The harness copies the production resource blocks into a provider-mocked fixture.
mock_provider "nebius" {
  mock_data "nebius_iam_v1_group" {
    defaults = {
      id = "group-e00r7z9vfxmg1bk99s"
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
    grafana           = { enabled = true }
    loki = {
      enabled = true
      region  = "eu-west2"
    }
  }
}

run "default_cluster_with_loki_enabled" {
  command = plan

  assert {
    condition     = length(nebius_iam_v2_access_key.loki_s3_key) == 1
    error_message = "The Loki-enabled plan must include its access key."
  }

  assert {
    condition     = nebius_iam_v2_access_key.loki_s3_key[0].name == "k8s-training.loki-s3-access-key" && nebius_iam_v2_access_key.loki_s3_key[0].secret_delivery_mode == "MYSTERY_BOX"
    error_message = "Loki must use the cluster-specific key name and MysteryBox delivery."
  }

  assert {
    condition     = nebius_iam_v2_access_key.grafana_key[0].name == "k8s-training.grafana-access-key"
    error_message = "Grafana must use the cluster-specific key name."
  }
}

run "additional_cluster_has_distinct_key_names" {
  command = plan

  variables {
    cluster_name = "training-b"
  }

  assert {
    condition     = nebius_iam_v2_access_key.loki_s3_key[0].name == "training-b.loki-s3-access-key" && nebius_iam_v2_access_key.grafana_key[0].name == "training-b.grafana-access-key"
    error_message = "Additional clusters must have distinct access-key names."
  }
}

run "legacy_module_caller_keeps_names" {
  command = plan

  variables {
    cluster_name = null
  }

  assert {
    condition     = nebius_iam_v2_access_key.loki_s3_key[0].name == "loki-s3-access-key" && nebius_iam_v2_access_key.grafana_key[0].name == "grafana-access-key"
    error_message = "Callers without a cluster name must retain their legacy access-key names."
  }
}
