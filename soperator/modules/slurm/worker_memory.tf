locals {
  # Collector presets are Kubernetes quantities, while worker sizing uses GiB.
  collector_memory_parts = {
    for name in ["logs_collector", "jail_logs_collector"] : name => regex(
      "^([+-]?(?:[0-9]+(?:\\.[0-9]*)?|\\.[0-9]+))(.*)$", local.selected_preset[name].memory
    )
  }
  memory_unit_bytes = {
    "" = 1, n = 1e-9, u = 1e-6, m = 1e-3,
    k  = 1e3, K = 1e3, M = 1e6, G = 1e9, T = 1e12, P = 1e15, E = 1e18,
    Ki = 1024, Mi = pow(1024, 2), Gi = pow(1024, 3),
    Ti = pow(1024, 4), Pi = pow(1024, 5), Ei = pow(1024, 6)
  }
  collector_memory_gibibytes = {
    for name, parts in local.collector_memory_parts : name => tonumber(parts[0]) * try(
      local.memory_unit_bytes[parts[1]], tonumber("1${parts[1]}")
    ) / pow(1024, 3)
  }

  worker_sidecar_memory = local.resources.munge.memory + (var.sssd_enabled ? local.resources.sssd.memory : 0)
  # The rebooter is always enabled in the generated Helm values, independently
  # of enable_node_configurator.
  worker_agent_memory = [for res in var.node_capacity.worker : (
    local.resources.kruise_daemon.memory
    + local.resources.node_configurator.requests.memory
    + (var.telemetry_enabled ? sum(values(local.collector_memory_gibibytes)) : 0)
    # GPU nodes still need the stock DCGM exporter when the Soperator exporter is disabled.
    + (res.gpus > 0 ? local.resources.dcgm_exporter.memory : 0)
  )]

  worker_memory = [for i, res in var.node_capacity.worker :
    floor(res.memory_gibibytes - local.worker_sidecar_memory - local.worker_agent_memory[i])
  ]
}
