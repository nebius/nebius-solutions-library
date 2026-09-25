resource "nebius_storage_v1_bucket" "kubeflow" {
  parent_id         = var.parent_id
  name              = join("-", ["kubeflow", local.release-suffix])
  versioning_policy = "DISABLED"
}

data "nebius_iam_v1_group" "editors" {
  name      = "editors"
  parent_id = var.tenant_id
}

resource "nebius_iam_v1_service_account" "kubeflow_bucket_sa" {
  parent_id = var.parent_id
  name      = join("-", [module.k8s-training.kube_cluster.name, "kubeflow-sa"])
  depends_on = [
    module.k8s-training
  ]
}

resource "nebius_iam_v1_group_membership" "kubeflow_bucket_sa-editor" {
  parent_id = data.nebius_iam_v1_group.editors.id
  member_id = nebius_iam_v1_service_account.kubeflow_bucket_sa.id
}

resource "nebius_iam_v2_access_key" "kubeflow_bucket_key" {
  parent_id   = var.parent_id
  name        = "kubeflow-s3-bucket-key"
  description = "Access key for kubeflow bucket"
  account = {
    service_account = {
      id = nebius_iam_v1_service_account.kubeflow_bucket_sa.id
    }
  }
}

module "k8s-training" {
  source = "../../k8s-training"

  tenant_id = var.tenant_id
  parent_id = var.parent_id
  subnet_id = var.subnet_id
  region    = var.region
  iam_token = var.iam_token

  ssh_user_name = local.config.ssh_user_name
  ssh_public_key = {
    key = local.config.ssh_public_key
  }
  cpu_nodes_count            = local.config.k8s_cluster.cpu_nodes_count
  gpu_nodes_count_per_group  = local.config.k8s_cluster.gpu_nodes_count_per_group
  gpu_node_groups            = local.config.k8s_cluster.gpu_node_groups
  cpu_nodes_platform         = local.config.k8s_cluster.cpu_nodes_platform
  cpu_nodes_preset           = local.config.k8s_cluster.cpu_nodes_preset
  gpu_nodes_platform         = local.config.k8s_cluster.gpu_nodes_platform
  gpu_nodes_preset           = local.config.k8s_cluster.gpu_nodes_preset
  enable_gpu_cluster         = local.config.k8s_cluster.enable_gpu_cluster
  infiniband_fabric          = local.config.k8s_cluster.infiniband_fabric
  gpu_nodes_driverfull_image = local.config.k8s_cluster.gpu_nodes_driverfull_image
  enable_k8s_node_group_sa   = local.config.k8s_cluster.enable_k8s_node_group_sa
  enable_prometheus          = local.config.k8s_cluster.enable_prometheus
  enable_loki                = local.config.k8s_cluster.enable_loki
  loki_access_key_id         = local.config.k8s_cluster.loki_access_key_id
  loki_secret_key            = local.config.k8s_cluster.loki_secret_key
  gpu_health_cheker          = local.config.k8s_cluster.gpu_health_cheker
}

resource "nebius_applications_v1alpha1_k8s_release" "argocd" {
  cluster_id = module.k8s-training.kube_cluster.id
  parent_id  = var.parent_id

  application_name = "argocd"
  namespace        = "argocd"
  product_slug     = "nebius/argo-cd"

  depends_on = [
    module.k8s-training
  ]
}

resource "random_password" "kubeflow_admin" {
  length           = 25
  special          = true
  upper            = true
  lower            = true
  override_special = "@#$%"
}

resource "random_password" "kubeflow_user" {
  length           = 25
  special          = true
  upper            = true
  lower            = true
  override_special = "@#$%"
}

resource "nebius_applications_v1alpha1_k8s_release" "kubeflow" {
  cluster_id = module.k8s-training.kube_cluster.id
  parent_id  = var.parent_id

  application_name = "kubeflow"
  namespace        = "kubeflow"
  product_slug     = "nebius/kubeflow"

  set = {
    "storage_bucket_name" : nebius_storage_v1_bucket.kubeflow.name,
    "storage_endpoint_url" : "storage.${var.region}.nebius.cloud",
    "accessKey" : nebius_iam_v2_access_key.kubeflow_bucket_key.status.aws_access_key_id,
    "secretKey" : nebius_iam_v2_access_key.kubeflow_bucket_key.status.secret,
    "kubeflow_admin_password" : random_password.kubeflow_admin.result,
    "kubeflow_user_password" : random_password.kubeflow_user.result,
    "kubeflow_hostname" : local.config.kubeflow.kubeflow_hostname
  }

  depends_on = [
    nebius_storage_v1_bucket.kubeflow,
    nebius_applications_v1alpha1_k8s_release.argocd
  ]
}
