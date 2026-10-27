locals {
  const = {
    filesystem = {
      jail                 = "jail"
      controller_spool     = "controller-spool"
      jail_submount_prefix = "jail-submount"
      accounting           = "accounting"
    }
  }

  name = {
    filesystem = {
      jail = join("-", [
        trimsuffix(
          substr(
            var.k8s_cluster_name,
            0,
            64 - (length(local.const.filesystem.jail) + 1)
          ),
          "-"
        ),
        local.const.filesystem.jail
      ])

      controller_spool = join("-", [
        trimsuffix(
          substr(
            var.k8s_cluster_name,
            0,
            64 - (length(local.const.filesystem.controller_spool) + 1)
          ),
          "-"
        ),
        local.const.filesystem.controller_spool
      ])
      accounting = join("-", [
        trimsuffix(
          substr(
            var.k8s_cluster_name,
            0,
            64 - (length(local.const.filesystem.accounting) + 1)
          ),
          "-"
        ),
        local.const.filesystem.accounting
      ])
    }

    jail_submount = { for submount in var.jail_submounts :
      submount.name => join("-", [
        trimsuffix(
          substr(
            var.k8s_cluster_name,
            0,
            64 - (length("${local.const.filesystem.jail_submount_prefix}-${submount.name}") + 1)
          ),
          "-"
        ),
        "${local.const.filesystem.jail_submount_prefix}-${submount.name}"
      ])
      if submount.spec != null
    }
  }
}
