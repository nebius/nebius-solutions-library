resource "helm_release" "gpu-operator" {
  name             = "gpu-operator"
  repository       = var.helm_repository
  chart            = "gpu-operator"
  namespace        = "gpu-operator"
  create_namespace = true
  version          = var.helm_version
  atomic           = true
  timeout          = 600

  set = concat([
    # Node Feature Discovery
    {
      name  = "nfd.enabled"
      value = var.nfd_enabled
    },

    # GPU Driver
    {
      name  = "driver.enabled"
      value = true
    },
    {
      name  = "driver.version"
      value = var.driver_version
    },

    # RDMA / GPUDirect for InfiniBand
    {
      name  = "driver.rdma.enabled"
      value = var.rdma_enabled
    },
    {
      name  = "driver.rdma.useHostMofed"
      value = var.rdma_use_host_mofed
    },

    # DCGM (Data Center GPU Manager)
    {
      name  = "dcgm.enabled"
      value = true
    },

    # DCGM Exporter for Prometheus metrics
    {
      name  = "dcgmExporter.enabled"
      value = var.dcgm_exporter_enabled
    },
    {
      name  = "dcgmExporter.serviceMonitor.enabled"
      value = var.dcgm_service_monitor_enabled
    },

    # GPU Feature Discovery
    {
      name  = "gfd.enabled"
      value = true
    },

    # Device Plugin
    {
      name  = "devicePlugin.enabled"
      value = true
    },

    # Container Toolkit
    {
      name  = "toolkit.enabled"
      value = true
    },

    # MIG Strategy
    {
      name  = "mig.strategy"
      value = var.mig_strategy
    },

    # MIG Manager
    {
      name  = "migManager.enabled"
      value = var.mig_strategy != "none"
    },

    # Validator to ensure GPU stack is working
    {
      name  = "validator.enabled"
      value = true
    },

    # Sandbox workloads support (vGPU)
    {
      name  = "sandboxWorkloads.enabled"
      value = false
    },

    # GPU Direct Storage (optional)
    {
      name  = "gds.enabled"
      value = var.gds_enabled
    },
    ], var.cdi_enabled == null ? [] : [
    {
      name  = "cdi.enabled"
      value = var.cdi_enabled
    },
  ])
}
