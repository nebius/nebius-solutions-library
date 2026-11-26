mock_provider "helm" {}
mock_provider "kubernetes" {}
mock_provider "local" {}

variables {
  name                       = "accounting-resources-test"
  cluster_name               = "accounting-resources-test"
  operator_version           = "5.0.0"
  iam_tenant_id              = "tenant-test"
  iam_project_id             = "project-test"
  k8s_cluster_context        = "test-context"
  k8s_cluster_id             = "mk8scluster-test"
  flux_namespace             = "flux-system"
  login_ssh_root_public_keys = ["ssh-ed25519 test"]
  accounting_enabled         = true
  sssd_enabled               = true
  telemetry_enabled          = false
  backups_config             = null
  node_count = {
    controller = 1
    login      = 1
    worker     = [1]
  }
  node_capacity = {
    system     = { cpu_cores = 16, memory_gibibytes = 64, ephemeral_storage_gibibytes = 128 }
    controller = { cpu_cores = 16, memory_gibibytes = 64, ephemeral_storage_gibibytes = 128 }
    login      = { cpu_cores = 16, memory_gibibytes = 64, ephemeral_storage_gibibytes = 128 }
    worker     = [{ cpu_cores = 16, memory_gibibytes = 64, ephemeral_storage_gibibytes = 128, gpus = 0 }]
    accounting = { cpu_cores = 7, memory_gibibytes = 26, ephemeral_storage_gibibytes = 60 }
  }
  filestores = {
    jail           = { size_gibibytes = 1024, device = "virtiofs" }
    jail_submounts = []
  }
}

run "accounting_leaves_room_for_sssd_and_rebooter" {
  command = plan

  plan_options {
    target = [helm_release.soperator_fluxcd_cm]
  }

  assert {
    condition = yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).slurmCluster.values.slurmNodes.accounting.slurmdbd.resources == {
      cpu              = "4200m"
      memory           = "12.75Gi"
      ephemeralStorage = "34Gi"
    }
    error_message = "The 8-vCPU accounting fixture must leave room for MariaDB, munge, SSSD, and the rebooter without consuming the platform reserve."
  }

  assert {
    condition = yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).soperator.values.controllerManager.manager.env == {
      isMariadbCrdInstalled    = var.accounting_enabled
      isPrometheusCrdInstalled = var.telemetry_enabled
    }
    error_message = "Operator discovery flags must match the CRDs installed by this Pro profile."
  }
}

run "disabled_sssd_releases_only_its_own_reservation" {
  command = plan

  variables {
    sssd_enabled = false
  }

  plan_options {
    target = [helm_release.soperator_fluxcd_cm]
  }

  assert {
    condition = yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).slurmCluster.values.slurmNodes.accounting.slurmdbd.resources == {
      cpu              = "4400m"
      memory           = "13.25Gi"
      ephemeralStorage = "39Gi"
    }
    error_message = "Disabling SSSD must retain the rebooter reservation."
  }
}

run "effective_overrides_are_reserved" {
  command = plan

  variables {
    component_overrides = {
      mariadb = { cpu = 3, memory = 8, ephemeral_storage = 8 }
      node_configurator = {
        requests = { cpu = 1, memory = 0.75 }
        limits   = { memory = 1 }
      }
    }
  }

  plan_options {
    target = [helm_release.soperator_fluxcd_cm]
  }

  assert {
    condition = yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).slurmCluster.values.slurmNodes.accounting.slurmdbd.resources == {
      cpu              = "2700m"
      memory           = "16.25Gi"
      ephemeralStorage = "42Gi"
    }
    error_message = "Accounting sizing must use the effective MariaDB and rebooter requests after overrides."
  }
}

run "legacy_node_configurator_flag_keeps_rebooter_reservation" {
  command = plan

  variables {
    enable_node_configurator = false
  }

  plan_options {
    target = [helm_release.soperator_fluxcd_cm]
  }

  assert {
    condition     = yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).slurmCluster.values.slurmNodes.accounting.slurmdbd.resources.cpu == "4200m"
    error_message = "The generated values always enable the rebooter, independently of enable_node_configurator."
  }
}

run "disabled_accounting_needs_no_accounting_node" {
  command = plan

  variables {
    accounting_enabled = false
    node_capacity = {
      system     = { cpu_cores = 16, memory_gibibytes = 64, ephemeral_storage_gibibytes = 128 }
      controller = { cpu_cores = 16, memory_gibibytes = 64, ephemeral_storage_gibibytes = 128 }
      login      = { cpu_cores = 16, memory_gibibytes = 64, ephemeral_storage_gibibytes = 128 }
      worker     = [{ cpu_cores = 16, memory_gibibytes = 64, ephemeral_storage_gibibytes = 128, gpus = 0 }]
    }
  }

  plan_options {
    target = [helm_release.soperator_fluxcd_cm]
  }

  assert {
    condition     = !yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).slurmCluster.values.slurmNodes.accounting.enabled
    error_message = "Disabled accounting must not require an accounting node capacity."
  }

  assert {
    condition = yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).soperator.values.controllerManager.manager.env == {
      isMariadbCrdInstalled    = var.accounting_enabled
      isPrometheusCrdInstalled = var.telemetry_enabled
    }
    error_message = "Operator discovery flags must match the CRDs installed by this Pro profile."
  }
}

run "reject_exhausted_cpu_after_overrides" {
  command = plan

  variables {
    component_overrides = {
      mariadb = { cpu = 6.2, memory = 12, ephemeral_storage = 16 }
    }
  }

  plan_options {
    target = [helm_release.soperator_fluxcd_cm]
  }

  expect_failures = [helm_release.soperator_fluxcd_cm]
}

run "reject_exhausted_memory_after_overrides" {
  command = plan

  variables {
    component_overrides = {
      mariadb = { cpu = 2, memory = 25, ephemeral_storage = 16 }
    }
  }

  plan_options {
    target = [helm_release.soperator_fluxcd_cm]
  }

  expect_failures = [helm_release.soperator_fluxcd_cm]
}

run "reject_exhausted_storage_after_overrides" {
  command = plan

  variables {
    component_overrides = {
      mariadb = { cpu = 2, memory = 12, ephemeral_storage = 55 }
    }
  }

  plan_options {
    target = [helm_release.soperator_fluxcd_cm]
  }

  expect_failures = [helm_release.soperator_fluxcd_cm]
}

run "telemetry_and_notifier_use_native_values" {
  command = plan

  variables {
    telemetry_enabled = true
    soperator_notifier = {
      enabled           = true
      slack_webhook_url = "https://example.invalid/pro-notifier"
    }
  }

  plan_options {
    target = [helm_release.soperator_fluxcd_cm]
  }

  assert {
    condition = yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).soperator.values.controllerManager.manager.env == {
      isMariadbCrdInstalled    = var.accounting_enabled
      isPrometheusCrdInstalled = var.telemetry_enabled
    }
    error_message = "Operator discovery flags must match the CRDs installed by this Pro profile."
  }

  assert {
    condition = (
      !strcontains(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"], var.soperator_notifier.slack_webhook_url) &&
      yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).notifier.valuesFrom[0].kind == "Secret" &&
      yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).notifier.valuesFrom[0].name == yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[1].metadata.name &&
      yamldecode(base64decode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[1].data["values.yaml"])).slack.webhookUrl == var.soperator_notifier.slack_webhook_url &&
      yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).notifier.valuesFrom[2].name == "soperator-notifier"
    )
    error_message = "Use the notifier Secret before the existing external override sources."
  }
}

run "telemetry_without_accounting" {
  command = plan

  variables {
    telemetry_enabled  = true
    accounting_enabled = false
  }

  plan_options {
    target = [helm_release.soperator_fluxcd_cm]
  }

  assert {
    condition = yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).soperator.values.controllerManager.manager.env == {
      isMariadbCrdInstalled    = var.accounting_enabled
      isPrometheusCrdInstalled = var.telemetry_enabled
    }
    error_message = "Operator discovery flags must match the CRDs installed by this Pro profile."
  }
}
