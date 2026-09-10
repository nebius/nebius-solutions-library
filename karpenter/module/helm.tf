resource "helm_release" "karpenter" {
  name      = "karpenter"
  namespace = "karpenter"
  
  repository = "oci://cr.eu-north1.nebius.cloud/e00w67thrrz5nhprjm/helm"
  
  chart      = "karpenter"
  version    = "0.1.4"

  
  create_namespace = true
  atomic           = true

  values = [
    "${file("${path.module}/values.yaml")}"
  ]
  
  set = [
    {
      name  = "controller.image.tag"
      value = "0.1.4"
    },
    {
      name  = "controller.settings.parentID"
      value = data.nebius_iam_v1_project.project.id
    },
    {
      name  = "controller.settings.clusterID"
      value = nebius_mk8s_v1_cluster.cluster.id
    }
  ]
}
