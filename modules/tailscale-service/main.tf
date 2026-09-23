locals {
  use_local_exec_proxy_restart = (
    var.restart_generated_proxy_once_after_create &&
    var.restart_generated_proxy_strategy == "local_exec"
  )
  use_template_annotation_proxy_restart = (
    var.restart_generated_proxy_once_after_create &&
    var.restart_generated_proxy_strategy == "template_annotations"
  )
  generated_proxy_label_selector = join(",", [
    "tailscale.com/managed=true",
    "tailscale.com/parent-resource=${var.name}",
    "tailscale.com/parent-resource-ns=${var.namespace}",
    "tailscale.com/parent-resource-type=svc",
  ])
  tailnet_ingress_hostname = try([
    for ingress in kubernetes_service_v1.this.status[0].load_balancer[0].ingress :
    ingress.hostname
    if try(trimspace(ingress.hostname), "") != ""
  ][0], null)

  service_annotations = merge(
    var.additional_annotations,
    {
      "tailscale.com/hostname" = var.tailnet_hostname
      "tailscale.com/tags"     = join(",", var.tags)
    }
  )
}

resource "kubernetes_service_v1" "this" {
  metadata {
    name        = var.name
    namespace   = var.namespace
    annotations = local.service_annotations
  }

  wait_for_load_balancer = local.use_template_annotation_proxy_restart

  spec {
    load_balancer_class = var.load_balancer_class
    type                = var.type
    selector            = var.selector

    dynamic "port" {
      for_each = var.ports

      content {
        name         = try(port.value.name, null)
        port         = port.value.port
        target_port  = port.value.target_port
        protocol     = try(port.value.protocol, "TCP")
        app_protocol = try(port.value.app_protocol, null)
      }
    }
  }
}

data "kubernetes_resources" "generated_proxy_statefulset" {
  count = local.use_template_annotation_proxy_restart ? 1 : 0

  api_version    = "apps/v1"
  kind           = "StatefulSet"
  namespace      = var.operator_namespace
  label_selector = local.generated_proxy_label_selector
  limit          = 2

  depends_on = [kubernetes_service_v1.this]
}

resource "kubernetes_annotations" "restart_generated_proxy_statefulset" {
  count = local.use_template_annotation_proxy_restart ? 1 : 0

  api_version = "apps/v1"
  kind        = "StatefulSet"
  force       = true

  metadata {
    name      = one([for object in data.kubernetes_resources.generated_proxy_statefulset[0].objects : object.metadata.name])
    namespace = var.operator_namespace
  }

  template_annotations = {
    "tailscale.nebius.ai/restarted-for-service-uid" = kubernetes_service_v1.this.metadata[0].uid
  }

  lifecycle {
    # The Tailscale operator can reconcile away our one-time template annotation
    # after the rollout starts. Ignore that steady-state drift, but still
    # replace this resource when the Service UID changes so a Service
    # recreation triggers one new rollout patch.
    ignore_changes       = [template_annotations]
    replace_triggered_by = [kubernetes_service_v1.this.metadata[0].uid]
  }

  depends_on = [data.kubernetes_resources.generated_proxy_statefulset]
}

# This stays imperative by default because it can both restart the operator-
# managed proxy pod and wait for the recreated pod to become Ready.
resource "terraform_data" "restart_generated_proxy_once" {
  count = local.use_local_exec_proxy_restart ? 1 : 0

  triggers_replace = {
    service_uid = kubernetes_service_v1.this.metadata[0].uid
  }

  lifecycle {
    precondition {
      condition     = trimspace(coalesce(var.kubectl_context, "")) != ""
      error_message = "kubectl_context must be set when restart_generated_proxy_once_after_create is true."
    }
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    environment = {
      KUBECTL_CONTEXT = var.kubectl_context
      PROXY_NAMESPACE = var.operator_namespace
      SERVICE_NAME    = var.name
      SERVICE_NS      = var.namespace
      TIMEOUT_SECONDS = tostring(var.proxy_restart_timeout_seconds)
    }

    command = <<-EOT
      set -euo pipefail

      selector="${local.generated_proxy_label_selector}"
      timeout_at=$((SECONDS + TIMEOUT_SECONDS))

      while true; do
        if kubectl --context "$KUBECTL_CONTEXT" -n "$PROXY_NAMESPACE" get pod -l "$selector" -o name 2>/dev/null | grep -q .; then
          break
        fi

        if [ "$SECONDS" -ge "$timeout_at" ]; then
          echo "Timed out waiting for generated Tailscale proxy pod for $SERVICE_NS/$SERVICE_NAME" >&2
          exit 1
        fi

        sleep 2
      done

      kubectl --context "$KUBECTL_CONTEXT" -n "$PROXY_NAMESPACE" delete pod -l "$selector" --wait=true

      timeout_at=$((SECONDS + TIMEOUT_SECONDS))
      while true; do
        if kubectl --context "$KUBECTL_CONTEXT" -n "$PROXY_NAMESPACE" get pod -l "$selector" -o name 2>/dev/null | grep -q .; then
          break
        fi

        if [ "$SECONDS" -ge "$timeout_at" ]; then
          echo "Timed out waiting for recreated Tailscale proxy pod for $SERVICE_NS/$SERVICE_NAME" >&2
          exit 1
        fi

        sleep 2
      done

      kubectl --context "$KUBECTL_CONTEXT" -n "$PROXY_NAMESPACE" wait --for=condition=Ready pod -l "$selector" --timeout="$TIMEOUT_SECONDS"s
    EOT
  }

  depends_on = [kubernetes_service_v1.this]
}
