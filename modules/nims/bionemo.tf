locals {
  bionemo_instances = local.bionemo_model.enabled ? {
    for index in range(local.bionemo_model.replicas) : tostring(index) => merge(local.bionemo_model, {
      index           = index
      deployment_name = "${local.bionemo_model.deployment_name}-${index}"
      service_name    = "${local.bionemo_model.service_name}-${index}"
      pod_label       = "${local.bionemo_model.deployment_name}-${index}"
    })
  } : {}
}

resource "kubernetes_deployment_v1" "bionemo_notebook" {
  for_each = local.bionemo_instances

  depends_on = [kubernetes_namespace_v1.nims]

  metadata {
    name      = each.value.deployment_name
    namespace = var.namespace

    labels = {
      app = each.value.app
      pod = each.value.pod_label
    }
  }

  spec {
    replicas = 1

    selector {
      match_labels = {
        pod = each.value.pod_label
      }
    }

    template {
      metadata {
        labels = {
          pod = each.value.pod_label
        }
      }

      spec {
        image_pull_secrets {
          name = kubernetes_secret_v1.nvcrio-cred.metadata[0].name
        }

        init_container {
          name    = "prepare-bionemo-workspace"
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
            name       = "workspace"
            mount_path = "/mnt/data"
          }
        }

        container {
          name    = each.value.container_name
          image   = "${each.value.image}:${each.value.version}"
          command = each.value.command

          port {
            container_port = each.value.container_port
          }

          resources {
            limits   = each.value.resources.limits
            requests = each.value.resources.requests
          }

          volume_mount {
            name       = "workspace"
            mount_path = each.value.cache_mount_path
            sub_path   = each.value.cache_sub_path
          }
        }

        volume {
          name = "workspace"

          host_path {
            path = "/mnt/data"
            type = "Directory"
          }
        }
      }
    }
  }
}

resource "kubernetes_service_v1" "bionemo_public" {
  for_each = local.bionemo_instances

  depends_on = [kubernetes_namespace_v1.nims]

  metadata {
    name      = each.value.service_name
    namespace = var.namespace
  }

  spec {
    type = each.value.service_type

    selector = {
      pod = each.value.pod_label
    }

    port {
      name        = "http"
      port        = each.value.service_port
      target_port = each.value.container_port
      protocol    = "TCP"
    }
  }
}
