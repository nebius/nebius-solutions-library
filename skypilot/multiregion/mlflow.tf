locals {
  create_mlflow_service_account = var.enable_mlflow_cluster && (var.mlflow_service_account_id == null || trimspace(var.mlflow_service_account_id) == "")
  mlflow_cluster_name_effective = coalesce(var.mlflow_cluster_name, local.multi_region ? "${var.cluster_name}-${local.primary_region}-mlflow" : "${var.cluster_name}-mlflow")
}

data "nebius_vpc_v1_subnet" "primary_for_mlflow" {
  count = var.enable_mlflow_cluster ? 1 : 0
  id    = local.primary_subnet_id
}

resource "random_password" "mlflow_admin_password" {
  count            = var.enable_mlflow_cluster && (var.mlflow_admin_password == null || trimspace(var.mlflow_admin_password) == "") ? 1 : 0
  length           = 24
  min_lower        = 1
  min_numeric      = 1
  min_special      = 1
  min_upper        = 1
  override_special = "-!@#$^&*_=+:;|/?,.`~()[]{}<>"
  special          = true
  upper            = true
  lower            = true
}

data "nebius_iam_v1_group" "editors_mlflow" {
  count     = local.create_mlflow_service_account ? 1 : 0
  name      = "editors"
  parent_id = var.tenant_id
}

resource "nebius_iam_v1_service_account" "mlflow" {
  count     = local.create_mlflow_service_account ? 1 : 0
  parent_id = local.primary_parent_id
  name      = local.multi_region ? "${var.cluster_name}-${local.primary_region}-mlflow-sa" : "${var.cluster_name}-mlflow-sa"
}

resource "nebius_iam_v1_group_membership" "mlflow_sa_admin" {
  count     = local.create_mlflow_service_account ? 1 : 0
  parent_id = data.nebius_iam_v1_group.editors_mlflow[0].id
  member_id = nebius_iam_v1_service_account.mlflow[0].id
}

resource "nebius_msp_mlflow_v1alpha1_cluster" "mlflow" {
  count = var.enable_mlflow_cluster ? 1 : 0

  parent_id   = local.primary_parent_id
  name        = local.mlflow_cluster_name_effective
  description = var.mlflow_cluster_description

  public_access      = var.mlflow_public_access
  admin_username     = var.mlflow_admin_username
  admin_password     = coalesce(var.mlflow_admin_password, random_password.mlflow_admin_password[0].result)
  service_account_id = local.create_mlflow_service_account ? nebius_iam_v1_service_account.mlflow[0].id : var.mlflow_service_account_id

  network_id          = data.nebius_vpc_v1_subnet.primary_for_mlflow[0].network_id
  size                = var.mlflow_size
  storage_bucket_name = var.mlflow_storage_bucket_name

  depends_on = [
    nebius_iam_v1_group_membership.mlflow_sa_admin
  ]
}
