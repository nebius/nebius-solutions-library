data "nebius_vpc_v1_subnet" "default-subnet" {
  id = var.subnet_id
}

resource "kubernetes_config_map_v1_data" "cilium-config" {
  metadata {
    name      = "cilium-config"
    namespace = "kube-system"
  }
  data = {
    "enable-ipv4-egress-gateway" = "true"
  }
}

resource "kubernetes_annotations" "restart_cilium_agent" {
  api_version = "apps/v1"
  kind        = "DaemonSet"
  metadata {
    name      = "cilium"
    namespace = "kube-system"
  }
  template_annotations = {
    "restarted_for" = "egress-gateway"
  }
  depends_on = [
    resource.kubernetes_config_map_v1_data.cilium-config
  ]
}

resource "kubernetes_annotations" "restart_cilium_operator" {
  api_version = "apps/v1"
  kind        = "Deployment"
  metadata {
    name      = "cilium-operator"
    namespace = "kube-system"
  }
  template_annotations = {
    "restarted_for" = "egress-gateway"
  }
  depends_on = [
    resource.kubernetes_config_map_v1_data.cilium-config
  ]
}

resource "nebius_vpc_v1_subnet" "egress-gateway-subnet" {
  network_id = data.nebius_vpc_v1_subnet.default-subnet.network_id
  parent_id  = var.project_id
  name       = "egress-gateway-subnet"

  ipv4_public_pools = {
    "pools" = [{
      "cidrs" = [
        { "cidr" = "/32" },
        { "cidr" = "/32" },
        { "cidr" = "/32" },
      ]
    }]
  }
}

resource "nebius_mk8s_v1_node_group" "egress-gateway" {
  fixed_node_count = 2
  parent_id        = var.mk8s_cluster_id
  name             = "k8s-ng-egress-gateway"
  labels = {
    "library-solution" : "k8s-training",
  }
  version = var.mk8s_version

  template = {
    metadata = {
      labels = {
        "io.cilium/egress-gateway" = "true"
      }
    }

    boot_disk = {
      size_gibibytes = var.nodes_disk_size
      type           = var.nodes_disk_type
    }
    cloud_init_user_data = templatefile("${path.module}/../cloud-init/k8s-cloud-init.tftpl", {
      enable_filestore     = "false",
      filestore_mount_path = "/mnt/data",
      ssh_user_name        = var.ssh_user_name,
      ssh_public_key       = var.ssh_public_key
    })
    network_interfaces = [
      {
        public_ip_address = {}
        subnet_id         = resource.nebius_vpc_v1_subnet.egress-gateway-subnet.id
      }
    ]
    resources = {
      platform = var.nodes_platform
      preset   = var.nodes_preset
    }

    taints = [{
      key    = "io.cilium/egress-gateway"
      value  = "true"
      effect = "NO_SCHEDULE"
    }]
  }
}

resource "kubectl_manifest" "egress_nodes_network_policy" {
  yaml_body = <<YAML
    apiVersion: cilium.io/v2
    kind: CiliumClusterwideNetworkPolicy
    metadata:
      name: restrict-nodeports-and-kubelet-egress-nodes
    spec:
      nodeSelector:
        matchLabels:
          io.cilium/egress-gateway: "true"
      ingress:
        - fromCIDR:
            - 10.0.0.0/8
            - 172.16.0.0/12
            - 192.168.0.0/16
          toPorts:
            - ports:
                - port: "10250"
                  protocol: TCP
                - port: "30000"
                  endPort: 32767
                  protocol: TCP
        - fromEntities:
            - cluster
            - kube-apiserver
      egress:
        - toEntities:
            - all
  YAML
}
