resource "helm_release" "create_soperatorchecks_user" {
  count = var.checks.create_soperatorchecks_user ? 1 : 0

  name       = "create-soperatorchecks-user-check"
  repository = local.helm.repository.raw
  chart      = local.helm.chart.raw
  version    = local.helm.version.raw

  create_namespace = true
  namespace        = var.slurm_cluster_namespace

  values = [templatefile("${path.module}/templates/create_user_soperatorchecks_action.yaml.tftpl", {
    slurm_cluster_namespace = var.slurm_cluster_namespace
    slurm_cluster_name      = var.slurm_cluster_name
  })]

  wait = true
}

resource "terraform_data" "wait_for_create_soperatorchecks_user" {
  depends_on = [
    helm_release.create_soperatorchecks_user
  ]

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command = join(
      " ",
      [
        "kubectl", "wait",
        "--for=jsonpath='{.status.k8sJobsStatus.lastJobStatus}'=Complete",
        "--timeout", "2m",
        "--context", var.k8s_cluster_context,
        "-n", var.slurm_cluster_namespace,
        "activechecks.slurm.nebius.ai/create-user-soperatorchecks-action"
      ]
    )
  }
}

resource "helm_release" "create_nebius_user" {
  count = var.checks.create_nebius_user ? 1 : 0

  depends_on = [ 
    terraform_data.wait_for_create_soperatorchecks_user 
  ]

  name       = "create-nebius-user-check"
  repository = local.helm.repository.raw
  chart      = local.helm.chart.raw
  version    = local.helm.version.raw

  create_namespace = true
  namespace        = var.slurm_cluster_namespace

  values = [templatefile("${path.module}/templates/create_user_check.yaml.tftpl", {
    slurm_cluster_namespace = var.slurm_cluster_namespace
    slurm_cluster_name      = var.slurm_cluster_name
    user_name               = var.checks.nebius_username
  })]

  wait = true
}

resource "helm_release" "install_package_check" {
  count = var.checks.install_package_check_enabled ? 1 : 0

  depends_on = [ 
    terraform_data.wait_for_create_soperatorchecks_user 
  ]

  name       = "install-package-check"
  repository = local.helm.repository.raw
  chart      = local.helm.chart.raw
  version    = local.helm.version.raw

  create_namespace = true
  namespace        = var.slurm_cluster_namespace

  values = [templatefile("${path.module}/templates/install_package.yaml.tftpl", {
    slurm_cluster_namespace = var.slurm_cluster_namespace
    slurm_cluster_name      = var.slurm_cluster_name
  })]

  wait = true
}

resource "helm_release" "ssh_check" {
  count = var.checks.ssh_check_enabled ? 1 : 0

  depends_on = [ 
    terraform_data.wait_for_create_soperatorchecks_user 
  ]

  name       = "ssh-check"
  repository = local.helm.repository.raw
  chart      = local.helm.chart.raw
  version    = local.helm.version.raw

  create_namespace = true
  namespace        = var.slurm_cluster_namespace

  values = [templatefile("${path.module}/templates/ssh_check.yaml.tftpl", {
    slurm_cluster_namespace = var.slurm_cluster_namespace
    slurm_cluster_name      = var.slurm_cluster_name
    num_of_login_nodes      = var.num_of_login_nodes
  })]

  wait = true
}

resource "helm_release" "upgrade_cuda" {
  count = var.checks.upgrade_cuda_enabled ? 1 : 0

  depends_on = [
    terraform_data.wait_for_create_soperatorchecks_user
  ]

  name       = "upgrade-cuda"
  repository = local.helm.repository.raw
  chart      = local.helm.chart.raw
  version    = local.helm.version.raw

  create_namespace = true
  namespace        = var.slurm_cluster_namespace

  values = [templatefile("${path.module}/templates/upgrade_cuda.yaml.tftpl", {
    slurm_cluster_namespace = var.slurm_cluster_namespace
    slurm_cluster_name      = var.slurm_cluster_name
    cuda_version            = var.checks.cuda_version
  })]

  wait = true
}

resource "terraform_data" "wait_for_checks" {
  depends_on = [
    helm_release.create_nebius_user,
    helm_release.install_package_check,
    helm_release.ssh_check,
    helm_release.upgrade_cuda,
  ]

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command = join(
      " ",
      [
        "kubectl", "wait",
        "--for=jsonpath='{.status.k8sJobsStatus.lastJobStatus}'=Complete",
        "--timeout", "10m",
        "--context", var.k8s_cluster_context,
        "-n", var.slurm_cluster_namespace,
        "activechecks.slurm.nebius.ai --all"
      ]
    )
  }
}
