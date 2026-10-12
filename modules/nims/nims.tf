resource "kubernetes_deployment_v1" "nims" {
  for_each = local.nim_models

  depends_on = [kubernetes_namespace_v1.nims]

  metadata {
    name      = each.value.deployment_name
    namespace = var.namespace
  }

  spec {
    replicas = each.value.enabled ? (each.value.scaling.enabled ? each.value.scaling.min_replicas : each.value.replicas) : 0

    selector {
      match_labels = {
        app = each.value.app
      }
    }

    template {
      metadata {
        labels = merge(
          {
            app      = each.value.app
            lb_group = each.value.lb_group
          },
          each.value.labels
        )
      }

      spec {
        image_pull_secrets {
          name = kubernetes_secret_v1.nvcrio-cred.metadata[0].name
        }

        init_container {
          name    = "prepare-nim-cache"
          image   = "busybox:1.36.1"
          command = ["mkdir", "-p", "/mnt/data/${each.value.cache_sub_path}"]

          resources {
            limits = {
              cpu    = "100m"
              memory = "32Mi"
            }
            requests = {
              cpu    = "10m"
              memory = "8Mi"
            }
          }

          volume_mount {
            name       = "mnt-data"
            mount_path = "/mnt/data"
          }
        }

        container {
          name    = each.value.container_name
          image   = "${each.value.image}:${each.value.version}"
          command = each.value.command

          dynamic "security_context" {
            for_each = each.value.security_context == null ? [] : [each.value.security_context]
            content {
              run_as_user  = try(security_context.value.run_as_user, null)
              run_as_group = try(security_context.value.run_as_group, null)
            }
          }

          env {
            name = "NGC_API_KEY"

            value_from {
              secret_key_ref {
                name = kubernetes_secret_v1.ngc_api_key.metadata[0].name
                key  = "NGC_API_KEY"
              }
            }
          }

          dynamic "env" {
            for_each = each.value.env
            content {
              name  = env.key
              value = env.value
            }
          }

          port {
            name           = "http"
            container_port = each.value.container_port
          }

          resources {
            limits   = each.value.resources.limits
            requests = each.value.resources.requests
          }

          volume_mount {
            name       = "dshm"
            mount_path = "/dev/shm"
          }

          volume_mount {
            name       = "mnt-data"
            mount_path = each.value.cache_mount_path
            sub_path   = each.value.cache_sub_path
          }
        }

        volume {
          name = "dshm"

          empty_dir {
            medium     = "Memory"
            size_limit = each.value.shared_memory_size
          }
        }

        volume {
          name = "mnt-data"

          host_path {
            path = "/mnt/data"
            type = "Directory"
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [spec[0].replicas]
  }
}

resource "kubernetes_service_v1" "nims" {
  for_each = local.nim_models

  depends_on = [kubernetes_namespace_v1.nims]

  metadata {
    name      = each.value.service_name
    namespace = var.namespace

    labels = {
      app                          = each.value.app
      "app.kubernetes.io/name"     = each.value.deployment_name
      "app.kubernetes.io/part-of"  = "nims"
      "app.kubernetes.io/instance" = each.key
    }
  }

  spec {
    selector = {
      app = each.value.app
    }

    port {
      name        = "http"
      port        = each.value.service_port
      target_port = each.value.container_port
    }

    type = "ClusterIP"
  }
}
