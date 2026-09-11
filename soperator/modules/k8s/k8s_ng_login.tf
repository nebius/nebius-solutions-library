resource "nebius_mk8s_v1_node_group" "login" {
  count = var.node_group_login.node_group_enabled ? 1 : 0

  depends_on = [
    nebius_mk8s_v1_cluster.this,
    nebius_mk8s_v1_node_group.system.this,
    terraform_data.check_resource_preset_sufficiency,
  ]

  parent_id = nebius_mk8s_v1_cluster.this.id

  version = var.k8s_version

  name = module.labels.name_nodeset_login
  labels = merge(
    module.labels.label_nodeset_login,
    module.labels.label_workload_cpu,
    module.labels.label_jail,
  )

  # These are broad infrastructure guardrails, not desired node counts: one
  # node keeps baseline login capacity available, while 100 caps runaway VM
  # provisioning. The Kubernetes autoscaler adds nodes only when login pods
  # are unschedulable and removes unused nodes down to the minimum.
  autoscaling = {
    min_node_count = 1
    max_node_count = 100
  }

  fixed_node_count = null

  template = {
    metadata = {
      labels = merge(
        module.labels.label_nodeset_login,
        module.labels.label_workload_cpu,
        module.labels.label_jail,
      )
    }
    taints = [{
      key    = module.labels.key_slurm_nodeset_name,
      value  = module.labels.name_nodeset_login
      effect = "NO_SCHEDULE"
    }]

    resources = {
      platform = var.node_group_login.resource.platform
      preset   = var.node_group_login.resource.preset
    }

    boot_disk = {
      type             = var.node_group_login.boot_disk.type
      size_bytes       = provider::units::from_gib(var.node_group_login.boot_disk.size_gibibytes)
      block_size_bytes = provider::units::from_kib(var.node_group_login.boot_disk.block_size_kibibytes)
    }

    filesystems = concat(
      [
        {
          attach_mode = "READ_WRITE"
          mount_tag   = var.filestores.jail.mount_tag
          existing_filesystem = {
            id = var.filestores.jail.id
          }
        }
      ],
      [
        for submount in var.filestores.jail_submounts :
        {
          attach_mode = "READ_WRITE"
          mount_tag   = submount.mount_tag
          existing_filesystem = {
            id = submount.id
          }
        }
      ]
    )

    network_interfaces = [
      {
        public_ip_address = local.node_ssh_access_public_ip.enabled ? {} : null
        subnet_id         = var.vpc_subnet_id
      }
    ]

    os = "ubuntu24.04"

    cloud_init_user_data = local.node_ssh_access.enabled ? local.node_cloud_init.cloud_init_data_no_nvidia : null
  }

  lifecycle {
    ignore_changes = [
      labels,
    ]
  }
}

resource "nebius_vpc_v1_allocation" "this" {
  lifecycle {
    create_before_destroy = true
  }

  parent_id = var.iam_project_id

  name = var.login_public_ip ? "${var.name}-public-static-ip" : "${var.name}-private-static-ip"

  ipv4_public = var.login_public_ip ? {
    cidr      = "/32"
    subnet_id = var.vpc_subnet_id
  } : null

  ipv4_private = var.login_public_ip ? null : {
    cidr      = "/32"
    subnet_id = var.vpc_subnet_id
  }
}
