module "weka" {
  count = 0

  source = "../../modules/weka"

  iam_project_id   = data.nebius_iam_v1_project.this.id
  k8s_cluster_name = local.k8s_cluster_name

  fs = [{
    name                 = "jail"
    size_gibibytes       = 2048
    block_size_kibibytes = 32
    forbid_deletion      = true
  }]
}
