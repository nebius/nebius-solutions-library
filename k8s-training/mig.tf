locals {
  mig_manager_enabled = (
    !local.gb300_enabled &&
    !local.use_driverfull_gpu &&
    var.gpu_node_groups > 0 &&
    contains(["single", "mixed"], coalesce(var.mig_strategy, "none"))
  )
  desired_mig_config = coalesce(var.mig_parted_config, "all-disabled")
  reconcile_mig_config = (
    local.mig_manager_enabled &&
    contains(
      lookup(local.valid_mig_parted_configs, local.gpu_nodes_platform, []),
      local.desired_mig_config,
    )
  )
  gpu_node_group_selector = local.reconcile_mig_config ? format(
    "nebius.com/node-group-id in (%s)",
    join(",", nebius_mk8s_v1_node_group.gpu[*].id),
  ) : ""
  mig_reconciler_image = "registry.k8s.io/kubectl:v${var.mig_reconciler_kubectl_version}"
  # Include the target group identities and executable in the one-shot request.
  # Same-group node replacement still bootstraps from template labels; this is
  # not a continuous node-health or autoscaling controller.
  mig_reconciliation_id = substr(sha256(jsonencode({
    config         = local.desired_mig_config
    node_group_ids = nebius_mk8s_v1_node_group.gpu[*].id
    image          = local.mig_reconciler_image
  })), 0, 12)
}

resource "kubernetes_service_account_v1" "mig_config_reconciler" {
  count = local.reconcile_mig_config ? 1 : 0

  metadata {
    name      = "mig-config-reconciler"
    namespace = "kube-system"
  }
}

resource "kubernetes_cluster_role_v1" "mig_config_reconciler" {
  count = local.reconcile_mig_config ? 1 : 0

  metadata {
    name = "mig-config-reconciler"
  }

  rule {
    api_groups = [""]
    resources  = ["nodes"]
    verbs      = ["get", "list", "patch", "watch"]
  }
}

resource "kubernetes_cluster_role_binding_v1" "mig_config_reconciler" {
  count = local.reconcile_mig_config ? 1 : 0

  metadata {
    name = "mig-config-reconciler"
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.mig_config_reconciler[0].metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.mig_config_reconciler[0].metadata[0].name
    namespace = kubernetes_service_account_v1.mig_config_reconciler[0].metadata[0].namespace
  }
}

resource "kubernetes_job_v1" "mig_config_reconciler" {
  count = local.reconcile_mig_config ? 1 : 0

  metadata {
    name      = "mig-config-reconciler-${local.mig_reconciliation_id}"
    namespace = kubernetes_service_account_v1.mig_config_reconciler[0].metadata[0].namespace
  }

  spec {
    active_deadline_seconds = 960
    backoff_limit           = 0

    template {
      metadata {}

      spec {
        service_account_name = kubernetes_service_account_v1.mig_config_reconciler[0].metadata[0].name
        restart_policy       = "Never"

        # Clear the previous request and its observed state together. This
        # prevents the convergence check from accepting a stale `success`
        # value before MIG Manager observes the next profile request.
        init_container {
          name  = "clear-previous-mig-config"
          image = local.mig_reconciler_image

          command = ["kubectl"]
          args = [
            "label",
            "nodes",
            "--selector=$(NODE_SELECTOR)",
            "nvidia.com/mig.config-",
            "nvidia.com/mig.config.state-",
          ]

          env {
            name  = "NODE_SELECTOR"
            value = local.gpu_node_group_selector
          }
        }

        # Re-adding the desired label is also NVIDIA's documented way to
        # retrigger MIG Manager after a failed profile application.
        init_container {
          name  = "request-mig-config"
          image = local.mig_reconciler_image

          command = ["kubectl"]
          args = [
            "label",
            "nodes",
            "--selector=$(NODE_SELECTOR)",
            "nvidia.com/mig.config=$(MIG_CONFIG)",
            "--overwrite",
          ]

          env {
            name  = "NODE_SELECTOR"
            value = local.gpu_node_group_selector
          }

          env {
            name  = "MIG_CONFIG"
            value = local.desired_mig_config
          }
        }

        container {
          name  = "wait-for-mig-config"
          image = local.mig_reconciler_image

          command = ["kubectl"]
          args = [
            "wait",
            "nodes",
            "--selector=$(NODE_SELECTOR)",
            "--for=jsonpath={.metadata.labels.nvidia\\.com/mig\\.config\\.state}=success",
            "--timeout=15m",
          ]

          env {
            name  = "NODE_SELECTOR"
            value = local.gpu_node_group_selector
          }
        }
      }
    }
  }

  wait_for_completion = true

  timeouts {
    create = "20m"
  }

  depends_on = [
    kubernetes_cluster_role_binding_v1.mig_config_reconciler,
    module.gpu-operator,
    module.gpu-operator-custom,
  ]
}
