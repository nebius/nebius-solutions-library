# NIM Metadata Service - Provides API for querying running NIMs

# ConfigMap with the application code
resource "kubernetes_config_map_v1" "metadata_service_code" {
  depends_on = [kubernetes_namespace_v1.nims]
  metadata {
    name      = "metadata-service-code"
    namespace = var.namespace
  }

  data = {
    "app.py" = file("${path.module}/metadata-service/app.py")
  }
}

# Service Account for the metadata service
resource "kubernetes_service_account_v1" "metadata_service" {
  depends_on = [kubernetes_namespace_v1.nims]
  metadata {
    name      = "nims-metadata-service"
    namespace = var.namespace
  }
}

# Role to read deployments and pods in the namespace
resource "kubernetes_role_v1" "metadata_service" {
  depends_on = [kubernetes_namespace_v1.nims]
  metadata {
    name      = "nims-metadata-service"
    namespace = var.namespace
  }

  rule {
    api_groups = ["apps"]
    resources  = ["deployments"]
    verbs      = ["get", "list"]
  }

  rule {
    api_groups = [""]
    resources  = ["pods"]
    verbs      = ["get", "list"]
  }
}

# ClusterRole to read nodes (for GPU info)
resource "kubernetes_cluster_role_v1" "metadata_service" {
  metadata {
    name = "nims-metadata-service-nodes"
  }

  rule {
    api_groups = [""]
    resources  = ["nodes"]
    verbs      = ["get", "list"]
  }
}

# RoleBinding
resource "kubernetes_role_binding_v1" "metadata_service" {
  depends_on = [kubernetes_namespace_v1.nims]
  metadata {
    name      = "nims-metadata-service"
    namespace = var.namespace
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.metadata_service.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.metadata_service.metadata[0].name
    namespace = var.namespace
  }
}

# ClusterRoleBinding for node access
resource "kubernetes_cluster_role_binding_v1" "metadata_service" {
  metadata {
    name = "nims-metadata-service-nodes"
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.metadata_service.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.metadata_service.metadata[0].name
    namespace = var.namespace
  }
}

# Deployment
resource "kubernetes_deployment_v1" "metadata_service" {
  depends_on = [kubernetes_namespace_v1.nims]
  metadata {
    name      = "nims-metadata-service"
    namespace = var.namespace
  }

  spec {
    replicas = 1

    selector {
      match_labels = {
        app = "nims-metadata-service"
      }
    }

    template {
      metadata {
        labels = {
          app = "nims-metadata-service"
        }
      }

      spec {
        service_account_name = kubernetes_service_account_v1.metadata_service.metadata[0].name

        container {
          name  = "metadata-service"
          image = "python:3.11-slim"

          command = ["/bin/bash", "-c"]
          args = [
            "pip install --no-cache-dir fastapi uvicorn kubernetes pydantic requests && uvicorn app:app --host 0.0.0.0 --port 8080"
          ]

          working_dir = "/app"

          env {
            name  = "NAMESPACE"
            value = var.namespace
          }

          env {
            name  = "PROMETHEUS_URL"
            value = "http://prometheus-server.o11y.svc.cluster.local:80"
          }

          env {
            name  = "NIM_PORTS_JSON"
            value = jsonencode(local.nim_proxy_ports)
          }

          port {
            container_port = 8080
          }

          resources {
            limits = {
              cpu    = "500m"
              memory = "512Mi"
            }
            requests = {
              cpu    = "100m"
              memory = "256Mi"
            }
          }

          volume_mount {
            name       = "app-code"
            mount_path = "/app/app.py"
            sub_path   = "app.py"
          }
        }

        volume {
          name = "app-code"
          config_map {
            name = kubernetes_config_map_v1.metadata_service_code.metadata[0].name
          }
        }
      }
    }
  }
}

# ClusterIP Service
resource "kubernetes_service_v1" "metadata_service" {
  depends_on = [kubernetes_namespace_v1.nims]
  metadata {
    name      = "metadata-service-svc"
    namespace = var.namespace
  }

  spec {
    selector = {
      app = "nims-metadata-service"
    }

    port {
      port        = 8080
      target_port = 8080
    }

    type = "ClusterIP"
  }
}
