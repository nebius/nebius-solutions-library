resource "kubectl_manifest" "default-nodeclass" {
  yaml_body = yamlencode({
    "apiVersion" = "karpenter.k8s.nebius/v1alpha1"
    "kind"       = "NebiusNodeClass"
    "metadata" = {
      "name" = "default"
    }
    "spec" = {
      "subnetID" = data.nebius_vpc_v1_subnet.subnet.id
      "imageFamily" = {
        "imageFamily" = "mk8s-worker-node-v-1-31"
        "parentID" = "project-e00public-images"
      }
      "serviceAccountID" = nebius_iam_v1_service_account.node-sa.id
    }
  })

  depends_on = [
    helm_release.karpenter
  ]
}

resource "kubectl_manifest" "driverful-gpu" {
  yaml_body = yamlencode({
    "apiVersion" = "karpenter.k8s.nebius/v1alpha1"
    "kind"       = "NebiusNodeClass"
    "metadata" = {
      "name" = "driverful-gpu"
    }
    "spec" = {
      "subnetID" = data.nebius_vpc_v1_subnet.subnet.id
      "imageFamily" = {
        "imageFamily" = "mk8s-worker-node-v-1-31-cuda12"
        "parentID" = "project-e00public-images"
      }
      "serviceAccountID" = nebius_iam_v1_service_account.node-sa.id
    }
  })

  depends_on = [
    helm_release.karpenter
  ]
}

