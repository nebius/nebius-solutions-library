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

run "disabled_by_default" {
  command = plan

  plan_options {
    target = [helm_release.soperator_fluxcd_cm]
  }

  assert {
    condition     = !can(yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).slurmCluster.values.slurmNodes.login.docker)
    error_message = "Login Docker must remain disabled without an image disk."
  }

  assert {
    condition     = yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).slurmCluster.values.slurmNodes.login.userIsolation.enabled
    error_message = "User isolation must preserve the chart's enabled default independently of Docker."
  }
}

run "enabled_with_configurable_pvc" {
  command = plan

  variables {
    login_image_storage = {
      enabled = true
      spec = {
        size_gibibytes     = 930
        filesystem_type    = "xfs"
        storage_class_name = "compute-csi-network-ssd-io-m3-xfs"
      }
    }
  }

  plan_options {
    target = [helm_release.soperator_fluxcd_cm]
  }

  assert {
    condition = (
      yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).slurmCluster.values.slurmNodes.login.docker.enabled &&
      yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).customConfigmaps.values.docker.enabled
    )
    error_message = "Login-only Docker must enable both the login daemon and shared jail Docker support."
  }

  assert {
    condition = one([
      for mount in yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).slurmCluster.values.slurmNodes.login.volumes.jailSubMounts : mount
      if mount.name == "login-image-storage"
      ]) == {
      name      = "login-image-storage"
      mountPath = "/mnt/image-storage"
      volumeClaimTemplateSpec = {
        accessModes      = ["ReadWriteOnce"]
        storageClassName = "compute-csi-network-ssd-io-m3-xfs"
        resources        = { requests = { storage = "930Gi" } }
      }
    }
    error_message = "Each login pod must get a PVC template with the requested size and storage class."
  }
}

run "isolation_can_be_disabled_without_docker" {
  command = plan

  variables {
    login_user_isolation_enabled = false
  }

  plan_options {
    target = [helm_release.soperator_fluxcd_cm]
  }

  assert {
    condition     = !yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).slurmCluster.values.slurmNodes.login.userIsolation.enabled
    error_message = "The independent user-isolation setting must be rendered."
  }
}

run "docker_requires_isolation" {
  command = plan

  variables {
    login_user_isolation_enabled = false
    login_image_storage = {
      enabled = true
      spec = {
        size_gibibytes     = 93
        filesystem_type    = "ext4"
        storage_class_name = "compute-csi-network-ssd-io-m3-ext4"
      }
    }
  }

  plan_options {
    target = [helm_release.soperator_fluxcd_cm]
  }

  expect_failures = [var.login_user_isolation_enabled]
}

run "enabled_disk_requires_spec" {
  command = plan

  variables {
    login_image_storage = { enabled = true }
  }

  plan_options {
    target = [helm_release.soperator_fluxcd_cm]
  }

  expect_failures = [var.login_image_storage]
}
