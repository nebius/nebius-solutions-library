resource "kubernetes_namespace" "runai" {
  count = var.runai_cluster_enabled ? 1 : 0

  metadata {
    name = "runai"
  }

  depends_on = [
    nebius_mk8s_v1_cluster.k8s-cluster
  ]
}

# The runai-cluster chart pulls images from nvcr.io and expects an
# image-pull secret in its own namespace; the control plane's copy in
# runai-backend is namespace-scoped.
resource "kubernetes_secret" "runai_cluster_reg_creds" {
  count = var.runai_cluster_enabled ? 1 : 0

  metadata {
    name      = "runai-reg-creds"
    namespace = kubernetes_namespace.runai[0].metadata[0].name
  }

  type = "kubernetes.io/dockerconfigjson"

  data = {
    ".dockerconfigjson" = local.nvcr_dockerconfigjson
  }
}

resource "helm_release" "runai_cluster" {
  count = var.runai_cluster_enabled ? 1 : 0

  name                = "runai-cluster"
  namespace           = kubernetes_namespace.runai[0].metadata[0].name
  repository          = "https://helm.ngc.nvidia.com/nvidia/runai"
  chart               = "runai-cluster"
  version             = var.runai_version
  repository_username = "$oauthtoken"
  repository_password = var.ngc_api_key
  wait                = true
  timeout             = var.helm_timeout
  cleanup_on_fail     = true

  values = [
    yamlencode({
      controlPlane = {
        url          = var.runai_control_plane_url
        clientSecret = var.runai_cluster_client_secret
      }
      cluster = {
        uid = var.runai_cluster_uid
        url = var.runai_cluster_url
      }
      clusterConfig = {
        global = {
          ingress = {
            ingressClass = "haproxy"
          }
        }
      }
    })
  ]

  depends_on = [
    kubernetes_secret.runai_cluster_reg_creds,
    helm_release.runai_backend,
    # GPU workloads need the device plugin from the GPU operator.
    module.gpu-operator
  ]
}
