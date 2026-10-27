resource "kubernetes_namespace_v1" "nccl-test" {
  for_each = toset([
    "nccl-test",
    "kubeflow",
  ])

  metadata {
    name = each.key
  }
}

resource "kubernetes_service_account_v1" "nccl-test" {
  depends_on = [kubernetes_namespace_v1.nccl-test]
  metadata {
    name      = "nccl-test"
    namespace = "nccl-test"
  }
}

resource "kubernetes_cluster_role_binding_v1" "nccl-test" {
  metadata {
    name = "nccl-test"
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = "cluster-admin"
  }

  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.nccl-test.metadata[0].name
    namespace = "nccl-test"
  }

  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.nccl-test.metadata[0].name
    namespace = "kubeflow"
  }
}

resource "kubernetes_job_v1" "kubeflow-install" {
  depends_on = [
    kubernetes_cluster_role_binding_v1.nccl-test,
    kubernetes_namespace_v1.nccl-test,
  ]

  metadata {
    name      = "kubeflow-install"
    namespace = "nccl-test"
  }

  spec {
    template {
      metadata {
        name = "kubeflow"
      }

      spec {
        service_account_name = kubernetes_service_account_v1.nccl-test.metadata[0].name
        container {
          name    = "kubectl"
          image   = "bitnami/kubectl" # Example image, replace with your desired image
          command = ["/bin/sh", "-c"]
          args = [
            "kubectl apply -k 'github.com/kubeflow/training-operator/manifests/overlays/standalone?ref=v1.7.0'",
          ]

          # Configure the container to run as root
          security_context {
            run_as_user = 0
          }
        }

        # Restart policy for the Pod
        restart_policy = "Never"

        # Configure the Pod to run with elevated privileges
        security_context {
          run_as_user = 0
        }
      }
    }

    backoff_limit = 10
  }

  wait_for_completion = true

  timeouts {
    create = "30m"
  }
}

resource "helm_release" "nccl-test" {
  depends_on       = [kubernetes_job_v1.kubeflow-install]
  name             = "nccl-test"
  chart            = "${path.module}/files/helm/nccl-test"
  namespace        = "nccl-test"
  create_namespace = true
  atomic           = true
  wait_for_jobs    = true

  set = [
    {
      name  = "numberOfHosts"
      value = var.number_of_hosts
    },
    {
      name  = "image"
      value = var.image
    },
  ]
}

resource "kubernetes_job_v1" "wait-for-nccl-test" {
  depends_on = [helm_release.nccl-test]

  metadata {
    name      = "wait-for-nccl-test"
    namespace = "nccl-test"
  }

  spec {
    template {
      metadata {
        name = "wait-for-nccl-test"
      }

      spec {
        service_account_name = kubernetes_service_account_v1.nccl-test.metadata[0].name
        container {
          name    = "kubectl"
          image   = "bitnami/kubectl" # Example image, replace with your desired image
          command = ["/bin/sh", "-c"]
          args = [
            "kubectl wait --namespace nccl-test --for condition=Succeeded --timeout 1500s mpijob/nccl-test-nccl-test-h100",
          ]
        }

        # Restart policy for the Pod
        restart_policy = "Never"
      }
    }

    backoff_limit = 5
  }

  wait_for_completion = true

  timeouts {
    create = "30m"
  }
}
