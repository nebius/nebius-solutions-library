locals {
  # The generated Helm values always enable the rebooter, independently of
  # enable_node_configurator, so every accounting node needs its reservation.
  accounting_resources = var.accounting_enabled ? {
    # Keep CPU arithmetic in millicores so exhausted budgets remain exactly zero.
    cpu = (
      floor(var.node_capacity.accounting.cpu_cores * 1000)
      -ceil(local.resources.munge.cpu * 1000)
      -ceil(local.resources.mariadb.cpu * 1000)
      -(var.sssd_enabled ? ceil(local.resources.sssd.cpu * 1000) : 0)
      -ceil(local.resources.node_configurator.requests.cpu * 1000)
    ) / 1000
    memory = (
      var.node_capacity.accounting.memory_gibibytes
      -local.resources.munge.memory
      -local.resources.mariadb.memory
      -(var.sssd_enabled ? local.resources.sssd.memory : 0)
      -local.resources.node_configurator.requests.memory
    )
    ephemeral_storage = (
      var.node_capacity.accounting.ephemeral_storage_gibibytes
      -local.resources.munge.ephemeral_storage
      -local.resources.mariadb.ephemeral_storage
      -(var.sssd_enabled ? local.resources.sssd.ephemeral_storage : 0)
    )
  } : null
}
