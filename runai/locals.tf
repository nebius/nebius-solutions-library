locals {
  ssh_public_key = var.ssh_public_key.key != null ? var.ssh_public_key.key : (
    try(fileexists(var.ssh_public_key.path), false) ? file(var.ssh_public_key.path) : null
  )

  # Region defaults for node group platform/preset, same pattern as k8s-training.
  regions_default = {
    eu-west1 = {
      cpu_nodes_platform = "cpu-d3"
      cpu_nodes_preset   = "16vcpu-64gb"
      gpu_nodes_platform = "gpu-h200-sxm"
      gpu_nodes_preset   = "8gpu-128vcpu-1600gb"
    }
    eu-north1 = {
      cpu_nodes_platform = "cpu-d3"
      cpu_nodes_preset   = "16vcpu-64gb"
      gpu_nodes_platform = "gpu-h100-sxm"
      gpu_nodes_preset   = "8gpu-128vcpu-1600gb"
    }
    eu-north2 = {
      cpu_nodes_platform = "cpu-d3"
      cpu_nodes_preset   = "16vcpu-64gb"
      gpu_nodes_platform = "gpu-h200-sxm"
      gpu_nodes_preset   = "8gpu-128vcpu-1600gb"
    }
    us-central1 = {
      cpu_nodes_platform = "cpu-d3"
      cpu_nodes_preset   = "16vcpu-64gb"
      gpu_nodes_platform = "gpu-h200-sxm"
      gpu_nodes_preset   = "8gpu-128vcpu-1600gb"
    }
    me-west1 = {
      cpu_nodes_platform = "cpu-d3"
      cpu_nodes_preset   = "16vcpu-64gb"
      gpu_nodes_platform = "gpu-b200-sxm-a"
      gpu_nodes_preset   = "8gpu-160vcpu-1792gb"
    }
    uk-south1 = {
      cpu_nodes_platform = "cpu-d3"
      cpu_nodes_preset   = "16vcpu-64gb"
      gpu_nodes_platform = "gpu-b300-sxm"
      gpu_nodes_preset   = "8gpu-192vcpu-2768gb"
    }
    eu-west2 = {
      cpu_nodes_platform = "cpu-d3"
      cpu_nodes_preset   = "16vcpu-64gb"
      gpu_nodes_platform = "gpu-b300-sxm"
      gpu_nodes_preset   = "8gpu-192vcpu-2768gb"
    }
    us-north1 = {
      cpu_nodes_platform = "cpu-d3"
      cpu_nodes_preset   = "16vcpu-64gb"
      gpu_nodes_platform = "gpu-b300-sxm"
      gpu_nodes_preset   = "8gpu-192vcpu-2768gb"
    }
  }

  # Shared cloud-init template from modules/cloud-init (same template k8s-training uses).
  cloud_init_user_data = templatefile("${path.module}/../modules/cloud-init/k8s-cloud-init.tftpl", {
    enable_filestore         = "false",
    filestore_mount_path     = "",
    ssh_user_name            = var.ssh_user_name,
    ssh_public_key           = local.ssh_public_key,
    kubelet_numa_config      = null,
    kubelet_numa_config_yaml = ""
  })

  # Regions without a defaults entry resolve to null and are rejected by the
  # node-group preconditions in main.tf unless platform/preset are set explicitly.
  region_defaults = lookup(local.regions_default, var.region, {})

  cpu_nodes_platform = coalesce(var.cpu_nodes_platform, lookup(local.region_defaults, "cpu_nodes_platform", null))
  cpu_nodes_preset   = coalesce(var.cpu_nodes_preset, lookup(local.region_defaults, "cpu_nodes_preset", null))
  gpu_nodes_platform = coalesce(var.gpu_nodes_platform, lookup(local.region_defaults, "gpu_nodes_platform", null))
  gpu_nodes_preset   = coalesce(var.gpu_nodes_preset, lookup(local.region_defaults, "gpu_nodes_preset", null))

  # nvcr.io pull secret shared by the control plane (runai-backend namespace)
  # and the cluster-registration chart (runai namespace) — one definition so
  # the two copies cannot drift.
  nvcr_dockerconfigjson = jsonencode({
    auths = {
      "nvcr.io" = {
        username = "$oauthtoken"
        password = var.ngc_api_key
        auth     = base64encode("$oauthtoken:${var.ngc_api_key}")
      }
    }
  })

  # Base domain for the Run:ai UI (served at https://runai.<base domain>).
  # byo: user-supplied. self-signed: derived from the HAProxy ingress LB IP.
  # Extracted defensively: a nil or empty LB IP (e.g. plan/apply re-run while the
  # ingress Service is still pending, or a hostname instead of an IP) falls back
  # to an "unavailable" sentinel instead of hard-erroring — a tobool() error here
  # would block every plan and even terraform destroy. Readiness is
  # enforced by the check block below and by preconditions on the TLS cert and
  # the backend helm release, which never run during destroy.
  haproxy_lb_ip = try(data.kubernetes_service.haproxy.status[0].load_balancer[0].ingress[0].ip, null)
  runai_base_domain = var.tls_mode == "byo" ? var.runai_domain : (
    "${local.haproxy_lb_ip != null && local.haproxy_lb_ip != "" ? local.haproxy_lb_ip : "unavailable"}.sslip.io"
  )
}

# Warns (never fails) during plan/apply while the HAProxy Service has no LB IP
# yet; check blocks are not evaluated at all during destroy.
check "haproxy_lb_ip_ready" {
  assert {
    condition     = local.haproxy_lb_ip != null && local.haproxy_lb_ip != ""
    error_message = "HAProxy LoadBalancer IP is not assigned yet — re-run terraform apply once the ingress Service has an external IP (see README)."
  }
}
