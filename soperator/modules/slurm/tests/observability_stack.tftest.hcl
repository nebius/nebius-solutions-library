mock_provider "helm" {}
mock_provider "kubernetes" {}
mock_provider "local" {}

variables {
  name                       = "observability-stack-test"
  cluster_name               = "observability-stack-test"
  operator_version           = "5.1.0-r9e5600cd"
  iam_tenant_id              = "tenant-test"
  iam_project_id             = "project-test"
  k8s_cluster_context        = "test-context"
  k8s_cluster_id             = "mk8scluster-test"
  flux_namespace             = "flux-system"
  login_ssh_root_public_keys = ["ssh-ed25519 test"]
  accounting_enabled         = true
  telemetry_enabled          = true
  backups_config             = null
  nfs_in_k8s                 = { enabled = true, size_gibibytes = 93, threads = 8 }
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

run "private_metrics_and_persistent_logs" {
  command = plan

  plan_options {
    target = [helm_release.soperator_fluxcd_cm]
  }

  variables {
    public_o11y_enabled          = false
    create_pvcs                  = true
    use_preinstalled_gpu_drivers = true
  }

  assert {
    condition = (
      !can(yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).observability.vmStack.values.vmagent.spec.remoteWrite) &&
      !can(yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).observability.vmStack.dependsOn)
    )
    error_message = "A private installation must inherit only the local VMSingle destination and default prerequisites."
  }

  assert {
    condition = (
      yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).observability.vmLogs.values.server.persistentVolume.enabled &&
      yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).observability.vmStack.values.vmsingle.spec.storage.storageClassName == "compute-csi-network-ssd-io-m3-ext4" &&
      yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).observability.dcgmExporter.values.validateToolkit == false
    )
    error_message = "Persistent telemetry must keep the VMSingle storage class and VictoriaLogs claim. DCGM must skip toolkit validation for preinstalled drivers."
  }

  assert {
    condition     = [for resource in yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources : resource.metadata.name] == ["terraform-fluxcd-values"]
    error_message = "A persistent installation without a notifier must create only the umbrella inputs."
  }

  assert {
    condition = (
      alltrue([
        for collector in ["logs", "jailLogs", "events"] :
        yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).observability.opentelemetry[collector].values.podMonitor.enabled
      ]) &&
      yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).observability.ncclProfiles.collector.values.podMonitor.enabled &&
      yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).soperator.values.serviceMonitor.enabled &&
      yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).nfsServer.values.monitoring.enabled &&
      yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).nfsServer.values.monitoring.serviceMonitor.enabled
    )
    error_message = "With Prometheus CRDs installed, all four collectors, the operator and NFS must expose their monitors."
  }
}

run "telemetry_without_prometheus_crds" {
  command = plan

  variables {
    prometheus_crds_enabled = false
  }

  plan_options {
    target = [helm_release.soperator_fluxcd_cm]
  }

  assert {
    condition = (
      yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).observability.enabled &&
      !yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).observability.prometheusOperator.enabled &&
      !yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).soperator.values.controllerManager.manager.env.isPrometheusCrdInstalled
    )
    error_message = "Disabling the Prometheus CRDs must disable the release and operator discovery while keeping telemetry enabled."
  }

  assert {
    condition = (
      alltrue([
        for collector in ["logs", "jailLogs", "events"] :
        !yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).observability.opentelemetry[collector].values.podMonitor.enabled
      ]) &&
      !yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).observability.ncclProfiles.collector.values.podMonitor.enabled &&
      !yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).soperator.values.serviceMonitor.enabled &&
      yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).nfsServer.values.monitoring.enabled &&
      !yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).nfsServer.values.monitoring.serviceMonitor.enabled
    )
    error_message = "A cluster without Prometheus CRDs must keep the NFS exporter enabled and omit PodMonitor and ServiceMonitor resources."
  }
}

run "public_metrics_with_ephemeral_volumes" {
  command = plan

  plan_options {
    target = [helm_release.soperator_fluxcd_cm]
  }

  variables {
    public_o11y_enabled                       = true
    region                                    = "eu-north1"
    create_pvcs                               = false
    use_preinstalled_gpu_drivers              = false
    kube_state_metrics_max_scrape_size        = 314572800
    nccl_inspector_profiling                  = { enabled = true, dump_dir = "/custom/profiles" }
    opentelemetry_delete_jail_logs_after_read = false
    opentelemetry_batch                       = { send_batch_size = 3000 }
    opentelemetry_sending_queue               = { size = 2000, num_consumers = 6 }
    soperator_notifier                        = { enabled = true, slack_webhook_url = "test-webhook" }
  }

  assert {
    condition = [for destination in yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).observability.vmStack.values.vmagent.spec.remoteWrite : destination.url] == [
      "https://write.monitoring.eu-north1.nebius.cloud/projects/project-test/buckets/soperator/prometheus",
      "http://vmsingle-metrics-victoria-metrics-k8s-stack.monitoring-system.svc.cluster.local.:8428/api/v1/write",
    ] && yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).observability.vmStack.values.vmagent.spec.remoteWrite[0].bearerTokenSecret == { name = "o11y-writer-sa-token", key = "accessToken" }
    error_message = "Public metrics must retain the local VMSingle and authenticate to the selected project's public endpoint."
  }

  assert {
    condition = (
      !yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).observability.vmLogs.values.server.persistentVolume.enabled &&
      yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).observability.dcgmExporter.values.validateToolkit &&
      [for ref in yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).observability.vmStack.valuesFrom : ref.name] == ["terraform-vmsingle-ephemeral-storage", "terraform-victoria-metrics-operator", "victoria-metrics-operator"]
    )
    error_message = "Ephemeral metrics must pass the storage override ahead of existing Flux overlays. VictoriaLogs must disable its PVC, and DCGM must validate the toolkit."
  }

  assert {
    condition = { for resource in yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources : resource.metadata.name => resource.kind } == {
      terraform-fluxcd-values              = "ConfigMap"
      terraform-vmsingle-ephemeral-storage = "ConfigMap"
      terraform-notifier-credentials       = "Secret"
    } && yamldecode(one([for resource in yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources : resource.data["values.yaml"] if resource.metadata.name == "terraform-vmsingle-ephemeral-storage"])).vmsingle.spec.storage == null
    error_message = "The optional storage ConfigMap must coexist with notifier credentials and remove the downstream VMSingle default PVC."
  }

  assert {
    condition = (
      [for endpoint in yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).observability.vmStack.values.kube-state-metrics.vmScrape.spec.endpoints : endpoint.port] == ["http", "metrics"] &&
      yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).observability.vmStack.values.kube-state-metrics.vmScrape.spec.endpoints[0].max_scrape_size == "314572800"
    )
    error_message = "The native kube-state-metrics scrape must retain both endpoints and apply the per-job cap to HTTP."
  }

  assert {
    condition = (
      yamldecode(yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).observability.ncclProfiles.configMap.values.resources[0].data.relay).receivers["filelog/nccl_metrics"].include == ["/mnt/jail/custom/profiles/*/*/*.log"] &&
      !can(yamldecode(yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).observability.ncclProfiles.configMap.values.resources[0].data.relay).receivers["filelog/nccl_metrics"].delete_after_read) &&
      yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).observability.ncclProfiles.collector.values.extraVolumeMounts[0].readOnly
    )
    error_message = "The shared NCCL collector must read the configured jail directory without deleting these profiles."
  }

  assert {
    condition = (
      yamldecode(yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).observability.ncclProfiles.configMap.values.resources[0].data.relay).exporters["otlp_http/victoriametrics"].sending_queue.queue_size == 3000 &&
      yamldecode(yamldecode(yamldecode(helm_release.soperator_fluxcd_cm.values[0]).resources[0].data["values.yaml"]).observability.ncclProfiles.configMap.values.resources[0].data.relay).exporters["otlp_http/victoriametrics"].sending_queue.num_consumers == 6
    )
    error_message = "The NCCL persistent queue must fit at least one batch and preserve the configured consumer count."
  }
}
