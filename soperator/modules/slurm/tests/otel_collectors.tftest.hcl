mock_provider "helm" {}
mock_provider "kubernetes" {}
mock_provider "local" {}

variables {
  name                       = "collector-test"
  cluster_name               = "collector-test"
  operator_version           = "5.1.0-r9e5600cd"
  iam_tenant_id              = "tenant-test"
  iam_project_id             = "project-test"
  k8s_cluster_context        = "test-context"
  k8s_cluster_id             = "mk8scluster-test"
  flux_namespace             = "flux-system"
  login_ssh_root_public_keys = ["ssh-ed25519 test"]
  accounting_enabled         = false
  telemetry_enabled          = true
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

run "public_logs_keep_local_delivery_and_cloud_batch_limit" {
  command = plan

  variables {
    public_o11y_enabled                       = true
    opentelemetry_batch                       = { send_batch_size = 2500 }
    opentelemetry_sending_queue               = { size = 50, num_consumers = 3 }
    opentelemetry_delete_jail_logs_after_read = true
    opentelemetry_delete_jail_logs_min_age    = "9h"
  }

  plan_options {
    target = [helm_release.soperator_fluxcd_cm]
  }

  assert {
    condition = alltrue([
      for name, collector in yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).observability.opentelemetry :
      collector.valuesFrom[0].name == "terraform-opentelemetry-collector" &&
      collector.valuesFrom[0].valuesKey == "collector-values.yaml" &&
      collector.values.config.service.pipelines[name == "events" ? "logs/events" : "logs"].exporters == [
        name == "jailLogs" ? "otlp_http/victorialogs" : "otlp_http/victoriametrics", "otlp"
      ] &&
      collector.values.config.exporters.otlp.sending_queue.batch.max_size == 1000 &&
      collector.values.config.exporters.otlp.sending_queue.queue_size == 500 &&
      collector.values.config.exporters[name == "jailLogs" ? "otlp_http/victorialogs" : "otlp_http/victoriametrics"].sending_queue.queue_size == 2500
      if name != "enabled"
    ])
    error_message = "Each collector must keep local and regional Cloud Logging delivery, with independent batching limits and a queue large enough for each batch."
  }

  assert {
    condition = alltrue([
      for receiver in ["filelog", "filelog/health_checker"] :
      yamldecode(local.otel_collectors_yaml).jailLogs.values.config.receivers[receiver].start_at == "beginning" &&
      yamldecode(local.otel_collectors_yaml).jailLogs.values.config.receivers[receiver].delete_after_read_min_age == "9h"
    ])
    error_message = "Both jail receivers must read the whole file before deleting it after the requested retention period."
  }
}

run "private_logs_do_not_require_cloud_credentials" {
  command = plan

  variables {
    public_o11y_enabled                       = false
    opentelemetry_delete_jail_logs_after_read = false
  }

  plan_options {
    target = [helm_release.soperator_fluxcd_cm]
  }

  assert {
    condition = alltrue([
      for name, collector in yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).observability.opentelemetry :
      !can(collector.valuesFrom) && !can(collector.values.config.exporters.otlp)
      if name != "enabled"
    ])
    error_message = "Private collectors must not require the runtime discovery ConfigMap or a Cloud Logging exporter."
  }

  assert {
    condition = alltrue([
      for receiver in ["filelog", "filelog/health_checker"] :
      yamldecode(local.otel_collectors_yaml).jailLogs.values.config.receivers[receiver].start_at == "end" &&
      yamldecode(local.otel_collectors_yaml).jailLogs.values.config.receivers[receiver].delete_after_read == null
    ])
    error_message = "Both jail receivers must tail new files and leave them on disk when deletion is disabled."
  }
}
