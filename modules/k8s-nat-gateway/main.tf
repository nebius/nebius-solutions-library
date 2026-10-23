data "nebius_vpc_v1_subnet" "workers" {
  id = var.subnet_id
}

locals {
  namespace = "nat-gateway"

  node_selector = {
    "nebius.com/nat-gateway" = "true"
  }
  node_selector_string = join(",", [
    for key, value in local.node_selector : "${key}=${value}"
  ])

  # Keep gateways out of Services that use implicit node selection. The NAT
  # Service selects them explicitly, independently of this exclusion label.
  node_labels = merge(local.node_selector, {
    "nebius.com/exclude-from-implicit-load-balancer-selection" = "true"
  })

  node_taint = {
    key    = "nebius.com/nat-gateway"
    value  = "true"
    effect = "NO_SCHEDULE"
  }

  worker_cidrs = sort(distinct(flatten([
    for pool in coalesce(data.nebius_vpc_v1_subnet.workers.status.ipv4_private_pools, []) : coalesce(pool.cidrs, [])
  ])))
}

# Keep the gateways on an explicit direct-egress table so they cannot inherit
# the worker default route and send their own outbound traffic back to the LB.
resource "nebius_vpc_v1_route_table" "gateway_egress" {
  parent_id  = var.project_id
  network_id = data.nebius_vpc_v1_subnet.workers.network_id
  name       = "${var.name}-direct-egress"
}

resource "nebius_vpc_v1_route" "gateway_egress" {
  parent_id   = nebius_vpc_v1_route_table.gateway_egress.id
  name        = "${var.name}-direct-egress"
  destination = { cidr = "0.0.0.0/0" }
  next_hop    = { default_egress_gateway = true }
}

resource "nebius_vpc_v1_subnet" "gateway" {
  parent_id      = var.project_id
  network_id     = data.nebius_vpc_v1_subnet.workers.network_id
  name           = "${var.name}-subnet"
  route_table_id = nebius_vpc_v1_route_table.gateway_egress.id

  ipv4_private_pools = {
    use_network_pools = true
  }
  ipv4_public_pools = {
    use_network_pools = false
    pools = [{
      cidrs = [for index in range(var.public_ip_pool_size) : { cidr = "/32" }]
    }]
  }

  lifecycle {
    precondition {
      condition     = var.public_ip_pool_size >= var.node_count + 1
      error_message = "Reserve at least node_count + 1 public IPs to cover the one-node upgrade surge."
    }
  }
}

resource "nebius_mk8s_v1_node_group" "gateway" {
  parent_id        = var.mk8s_cluster_id
  name             = "${var.name}-nodes"
  fixed_node_count = var.node_count
  version          = var.mk8s_version
  labels           = { "library-solution" = "k8s-training" }

  strategy = {
    max_surge       = { count = 1 }
    max_unavailable = { count = 0 }
  }

  template = {
    metadata = { labels = local.node_labels }
    taints   = [local.node_taint]

    resources = {
      platform = var.nodes_platform
      preset   = var.nodes_preset
    }

    boot_disk = {
      size_gibibytes = var.nodes_disk_size
      type           = var.nodes_disk_type
    }

    network_interfaces = [{
      subnet_id         = nebius_vpc_v1_subnet.gateway.id
      public_ip_address = {}
    }]

    cloud_init_user_data = yamlencode(merge(
      yamldecode(templatefile("${path.module}/../cloud-init/k8s-cloud-init.tftpl", {
        enable_filestore         = "false"
        filestore_mount_path     = "/mnt/data"
        ssh_user_name            = var.ssh_user_name
        ssh_public_key           = var.ssh_public_key
        kubelet_numa_config      = null
        kubelet_numa_config_yaml = ""
      })),
      yamldecode(templatefile("${path.module}/files/nat-cloud-init.tftpl", {
        worker_cidrs = local.worker_cidrs
      }))
    ))
  }

  lifecycle {
    precondition {
      condition = length(local.worker_cidrs) > 0 && alltrue([
        for cidr in local.worker_cidrs : can(cidrnetmask(cidr))
      ])
      error_message = "The worker subnet must expose valid IPv4 source CIDRs for the NAT rules."
    }
  }

  depends_on = [
    nebius_vpc_v1_route.gateway_egress,
    kubectl_manifest.host_policy,
  ]
}

# The Service controller places its listener in the cluster control-plane subnet.
resource "nebius_vpc_v1_allocation" "load_balancer" {
  parent_id = var.project_id
  name      = "${var.name}-service-lb"
  ipv4_private = {
    subnet_id = var.cluster_subnet_id
    cidr      = "/32"
  }
}

# Leave route-table attachment to the configuration that owns the worker subnet.
resource "nebius_vpc_v1_route_table" "workers" {
  parent_id  = var.project_id
  network_id = data.nebius_vpc_v1_subnet.workers.network_id
  name       = "${var.name}-workers"
}

resource "nebius_vpc_v1_route" "workers" {
  parent_id   = nebius_vpc_v1_route_table.workers.id
  name        = "${var.name}-via-lb"
  destination = { cidr = "0.0.0.0/0" }
  next_hop = {
    allocation = { id = nebius_vpc_v1_allocation.load_balancer.id }
  }
}

# Public gateway addresses are for NAT egress; host ingress is cluster-only.
resource "kubectl_manifest" "host_policy" {
  yaml_body = <<-YAML
    apiVersion: cilium.io/v2
    kind: CiliumClusterwideNetworkPolicy
    metadata:
      name: nat-gateway-host-policy
    spec:
      nodeSelector:
        matchLabels: ${jsonencode(local.node_selector)}
      ingress:
        - fromEntities:
            - cluster
            - kube-apiserver
            - health
      egress:
        - toEntities:
            - all
  YAML
}

resource "kubectl_manifest" "namespace" {
  yaml_body = <<-YAML
    apiVersion: v1
    kind: Namespace
    metadata:
      name: ${local.namespace}
  YAML
}

resource "kubectl_manifest" "service" {
  # Wait for Service finalizers on deletion before releasing its allocation.
  wait      = true
  yaml_body = <<-YAML
    apiVersion: v1
    kind: Service
    metadata:
      name: nat-gateway-service
      namespace: ${local.namespace}
      annotations:
        nebius.com/load-balancer-type: "internal"
        nebius.com/load-balancer-target-node-selector: ${jsonencode(local.node_selector_string)}
        nebius.com/load-balancer-allocation-id: "${nebius_vpc_v1_allocation.load_balancer.id}"
    # No Pod selector: the gateway hosts forward routed traffic.
    spec:
      type: LoadBalancer
      externalTrafficPolicy: Cluster
      ports:
        - name: tcp
          protocol: TCP
          # Required Service frontend port. Routed NAT traffic uses the LB as
          # an L3 next hop and keeps its original destination IP and port;
          # forwarding is not limited to TCP/443.
          port: 443
          targetPort: 443
  YAML

  lifecycle {
    # Recreate through the Service API when replacing its reserved frontend.
    # Deletion waits for cloud-controller cleanup before releasing the old IP.
    replace_triggered_by = [nebius_vpc_v1_allocation.load_balancer.id]
  }

  depends_on = [
    kubectl_manifest.namespace,
    nebius_mk8s_v1_node_group.gateway,
  ]
}
