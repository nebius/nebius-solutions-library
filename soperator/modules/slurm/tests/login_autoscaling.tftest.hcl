mock_provider "helm" {}
mock_provider "kubernetes" {}
mock_provider "local" {}

variables {
  name                       = "login-storage-test"
  cluster_name               = "login-storage-test"
  operator_version           = "5.0.0"
  iam_tenant_id              = "tenant-test"
  iam_project_id             = "project-test"
  k8s_cluster_context        = "test-context"
  k8s_cluster_id             = "mk8scluster-test"
  flux_namespace             = "flux-system"
  login_ssh_root_public_keys = ["ssh-ed25519 test"]
  accounting_enabled         = false
  telemetry_enabled          = false
  backups_config             = null
  node_count = {
    controller = 1
    login      = 2
    worker     = [1]
  }
  node_capacity = {
    system     = { cpu_cores = 16, memory_gibibytes = 64, ephemeral_storage_gibibytes = 128 }
    controller = { cpu_cores = 16, memory_gibibytes = 64, ephemeral_storage_gibibytes = 128 }
    login      = { cpu_cores = 16, memory_gibibytes = 64, ephemeral_storage_gibibytes = 128 }
    worker     = [{ cpu_cores = 16, memory_gibibytes = 64, ephemeral_storage_gibibytes = 128, gpus = 0 }]
  }
  filestores = {
    jail           = { size_gibibytes = 1024, device = "virtiofs" }
    jail_submounts = []
  }
}

run "omitted" {
  command = plan
  plan_options {
    target = [helm_release.soperator_fluxcd_cm]
  }
  assert {
    condition     = !can(yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).slurmCluster.overrideValues.slurmNodes.login.autoscaling)
    error_message = "Login autoscaling must render the requested settings and defaults."
  }
  assert {
    condition     = yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).slurmCluster.overrideValues.slurmNodes.login.size == 2
    error_message = "Autoscaling must preserve the configured fixed login size."
  }
}

run "null" {
  command = plan
  variables {
    login_autoscaling = null
  }
  plan_options {
    target = [helm_release.soperator_fluxcd_cm]
  }
  assert {
    condition     = !can(yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).slurmCluster.overrideValues.slurmNodes.login.autoscaling)
    error_message = "Login autoscaling must render the requested settings and defaults."
  }
  assert {
    condition     = yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).slurmCluster.overrideValues.slurmNodes.login.size == 2
    error_message = "Autoscaling must preserve the configured fixed login size."
  }
}

run "defaults" {
  command = plan
  variables {
    login_autoscaling = {}
  }
  plan_options {
    target = [helm_release.soperator_fluxcd_cm]
  }
  assert {
    condition     = yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).slurmCluster.overrideValues.slurmNodes.login.autoscaling == { enabled = false, minReplicas = 1, maxReplicas = 10, targetCPUUtilizationPercentage = 70 }
    error_message = "Login autoscaling must render the requested settings and defaults."
  }
  assert {
    condition     = yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).slurmCluster.overrideValues.slurmNodes.login.size == 2
    error_message = "Autoscaling must preserve the configured fixed login size."
  }
}

run "enabled_defaults" {
  command = plan
  variables {
    login_autoscaling = { enabled = true }
  }
  plan_options {
    target = [helm_release.soperator_fluxcd_cm]
  }
  assert {
    condition     = yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).slurmCluster.overrideValues.slurmNodes.login.autoscaling == { enabled = true, minReplicas = 1, maxReplicas = 10, targetCPUUtilizationPercentage = 70 }
    error_message = "Login autoscaling must render the requested settings and defaults."
  }
  assert {
    condition     = yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).slurmCluster.overrideValues.slurmNodes.login.size == 2
    error_message = "Autoscaling must preserve the configured fixed login size."
  }
}

run "explicit" {
  command = plan
  variables {
    login_autoscaling = { enabled = true, min_size = 2, max_size = 3, target_cpu_utilization_percentage = 10 }
  }
  plan_options {
    target = [helm_release.soperator_fluxcd_cm]
  }
  assert {
    condition     = yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).slurmCluster.overrideValues.slurmNodes.login.autoscaling == { enabled = true, minReplicas = 2, maxReplicas = 3, targetCPUUtilizationPercentage = 10 }
    error_message = "Login autoscaling must render the requested settings and defaults."
  }
  assert {
    condition     = yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).slurmCluster.overrideValues.slurmNodes.login.size == 2
    error_message = "Autoscaling must preserve the configured fixed login size."
  }
}
