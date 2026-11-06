# The harness copies the production resource blocks into a provider-mocked fixture.
mock_provider "nebius" {
  override_during = plan
}
mock_provider "random" {}

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
  command = apply

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

  assert {
    condition = (
      nebius_iam_v1_group.loki_storage[0].parent_id == var.parent_id &&
      length(nebius_iam_v1_access_permit.loki_storage) == 6 &&
      alltrue([for permit in nebius_iam_v1_access_permit.loki_storage : contains([
        nebius_storage_v1_bucket.loki-bucket-chunks[0].id,
        nebius_storage_v1_bucket.loki-bucket-ruler[0].id,
        nebius_storage_v1_bucket.loki-bucket-admin[0].id,
      ], permit.resource_id) && contains(["storage.object-editor", "storage.viewer"], permit.role)])
    )
    error_message = "Loki access must be limited to its three buckets."
  }

  assert {
    condition = (
      nebius_iam_v1_group.grafana[0].parent_id == var.parent_id &&
      nebius_iam_v1_access_permit.grafana[0].resource_id == var.parent_id &&
      nebius_iam_v1_access_permit.grafana[0].role == "viewer" &&
      nebius_iam_v2_access_key.grafana_key[0].account.service_account.id == nebius_iam_v1_service_account.grafana[0].id &&
      nebius_iam_v2_access_key.grafana_key[0].account.service_account.id != var.k8s_node_group_sa_id
    )
    error_message = "Grafana must use a separate identity limited to its project."
  }
}

run "additional_cluster_has_distinct_key_names" {
  command = plan

  variables {
    cluster_name = "training-b"
    cluster_id   = "mk8scluster-e00r7z9vfxmg1bk98b"
  }

  assert {
    condition     = nebius_iam_v2_access_key.loki_s3_key[0].name == "training-b.loki-s3-access-key" && nebius_iam_v2_access_key.grafana_key[0].name == "training-b.grafana-access-key"
    error_message = "Additional clusters must have distinct access-key names."
  }

  assert {
    condition     = length(setintersection(output.identity_names, run.default_cluster_with_loki_enabled.identity_names)) == 0
    error_message = "Two clusters in the same project must have distinct identity and bucket names."
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
