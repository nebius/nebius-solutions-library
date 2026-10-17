resource "nebius_mk8s_v1_cluster" "k8s-cluster" {
  parent_id = var.parent_id
  name      = var.cluster_name
  control_plane = {
    endpoints = {
      public_endpoint = var.mk8s_cluster_public_endpoint ? { allowed_cidrs = var.mk8s_allowed_cidrs } : null
    }
    etcd_cluster_size = var.etcd_cluster_size
    subnet_id         = var.subnet_id
    version           = var.k8s_version
  }
}

data "nebius_iam_v1_group" "editors" {
  count = var.node_group_sa_editors ? 1 : 0

  name      = "editors"
  parent_id = var.tenant_id
}

resource "nebius_iam_v1_service_account" "k8s_node_group_sa" {
  parent_id = var.parent_id
  name      = "${var.cluster_name}-k8s-node-group-sa"
}

resource "nebius_iam_v1_group_membership" "k8s_node_group_sa-admin" {
  count = var.node_group_sa_editors ? 1 : 0

  parent_id = data.nebius_iam_v1_group.editors[0].id
  member_id = nebius_iam_v1_service_account.k8s_node_group_sa.id
}

################
# CPU NODE GROUP
################
resource "nebius_mk8s_v1_node_group" "cpu-only" {
  parent_id        = nebius_mk8s_v1_cluster.k8s-cluster.id
  name             = "${var.cluster_name}-ng-cpu"
  fixed_node_count = var.cpu_nodes_fixed_count
  version          = var.k8s_version
  labels = {
    "library-solution" : "runai",
  }
  template = {
    boot_disk = {
      size_gibibytes = var.cpu_disk_size
      type           = "NETWORK_SSD"
    }
    service_account_id = nebius_iam_v1_service_account.k8s_node_group_sa.id
    network_interfaces = [
      {
        public_ip_address = var.cpu_nodes_public_ips ? {} : null
        subnet_id         = var.subnet_id
      }
    ]
    resources = {
      platform = local.cpu_nodes_platform
      preset   = local.cpu_nodes_preset
    }
    underlay_required    = false
    cloud_init_user_data = local.cloud_init_user_data
  }
}

################
# GPU NODE GROUP
################
resource "nebius_mk8s_v1_node_group" "gpu" {
  parent_id        = nebius_mk8s_v1_cluster.k8s-cluster.id
  name             = "${var.cluster_name}-ng-gpu"
  fixed_node_count = var.gpu_nodes_fixed_count
  version          = var.k8s_version
  labels = {
    "library-solution" : "runai",
  }
  template = {
    boot_disk = {
      size_gibibytes = var.gpu_disk_size
      type           = "NETWORK_SSD"
    }
    service_account_id = nebius_iam_v1_service_account.k8s_node_group_sa.id
    network_interfaces = [
      {
        public_ip_address = var.gpu_nodes_public_ips ? {} : null
        subnet_id         = var.subnet_id
      }
    ]
    resources = {
      platform = local.gpu_nodes_platform
      preset   = local.gpu_nodes_preset
    }
    underlay_required    = false
    cloud_init_user_data = local.cloud_init_user_data
  }
}
