locals {
  repository = "oci://cr.eu-north1.nebius.cloud/soperator${var.operator_stable ? "" : "-unstable"}"
  values = merge(yamldecode(file("${path.module}/values.yaml")), {
    helmRepository = {
      soperator = { url = local.repository }
    }
    slurmCluster = {
      enabled   = false
      namespace = var.slurm_namespace
    }
    storageClasses = {
      enabled   = true
      version   = var.operator_version
      namespace = "storage-system"
    }
  })
}

# Seed only a new cluster. The Slurm module takes ownership of this ConfigMap
# through its raw Helm release after the foundation HelmReleases become ready.
# Never overwrite an existing full configuration with bootstrap-only values.
resource "terraform_data" "initial_values" {
  triggers_replace = [var.k8s_cluster_id]

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = "bash \"${path.module}/scripts/seed_values.sh\""
    environment = {
      K8S_CONTEXT = var.k8s_cluster_context
      NAMESPACE   = var.namespace
      VALUES_YAML = yamlencode(local.values)
    }
  }
}

resource "terraform_data" "foundation" {
  depends_on = [terraform_data.initial_values]

  triggers_replace = {
    cluster_id = var.k8s_cluster_id
    version    = var.operator_version
    values     = sha256(yamlencode(local.values))
    scripts = sha256(join("", [
      filesha256("${path.module}/scripts/install_foundation.sh"),
      filesha256("${path.module}/scripts/filter_foundation.sh"),
      filesha256("${path.module}/scripts/helm_with_filter.sh"),
      filesha256("${path.module}/templates/postrenderer-plugin.yaml"),
    ]))
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = "bash \"${path.module}/scripts/install_foundation.sh\""
    environment = {
      K8S_CONTEXT       = var.k8s_cluster_context
      NAMESPACE         = var.namespace
      STORAGE_NAMESPACE = local.values.storageClasses.namespace
      CHART_REPOSITORY  = local.repository
      CHART_VERSION     = var.operator_version
      VALUES_YAML       = yamlencode(local.values)
    }
  }
}

# This dependency is deliberately separate from output.ready: node groups need
# the foundation only; Flux takes over the umbrella after the full values exist.
resource "terraform_data" "full_values" {
  input = var.full_values_ready
}

resource "helm_release" "soperator_fluxcd_bootstrap" {
  depends_on = [terraform_data.foundation, terraform_data.full_values]

  name       = "soperator-fluxcd-bootstrap"
  repository = local.repository
  chart      = "helm-soperator-fluxcd-bootstrap"
  version    = var.operator_version
  namespace  = var.namespace
  # Child HelmReleases track their own readiness.
  wait = false

  values = [yamlencode({
    helmRepository = {
      url       = local.repository
      namespace = var.namespace
    }
    helmRelease = {
      namespace = var.namespace
      chart     = { version = var.operator_version }
    }
  })]
}

resource "terraform_data" "resume_umbrella" {
  depends_on = [helm_release.soperator_fluxcd_bootstrap]

  triggers_replace = [timestamp()]

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = "bash \"${path.module}/scripts/resume_umbrella.sh\""
    environment = {
      K8S_CONTEXT = var.k8s_cluster_context
      NAMESPACE   = var.namespace
    }
  }
}

output "ready" {
  value = terraform_data.foundation.id
}

output "bootstrap_ready" {
  value = terraform_data.resume_umbrella.id
}
