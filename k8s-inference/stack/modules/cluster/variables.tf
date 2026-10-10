variable "name" {
  description = "Cluster name; also the prefix of its node groups, identities, GPU clusters and filesystem."
  type        = string
}

variable "project_id" {
  description = "Nebius project the cluster and all of its resources are created in."
  type        = string
}

variable "subnet_id" {
  description = "Subnet of the project the control plane and the nodes attach to."
  type        = string
}

variable "kubernetes_version" {
  description = "Kubernetes version of the control plane and every node group."
  type        = string
}

variable "labels" {
  description = "Labels put on every cloud resource of the cluster."
  type        = map(string)
  default     = {}
}

variable "control_plane_allowed_cidrs" {
  description = "CIDRs that may reach the public Kubernetes API endpoint. Empty = open."
  type        = list(string)
  default     = []
}

variable "system_pool" {
  description = "The CPU node group that runs the platform add-ons (gateway, caches, controllers)."
  type = object({
    platform   = string
    preset     = string
    node_count = number
  })
}

variable "gpu_pools" {
  description = "GPU node groups keyed by pool name; the shape of `regions.*.pools` in terraform.tfvars (stack/config/variables.tf)."
  type = map(object({
    platform  = string
    preset    = string
    gpu_class = string
    capacity = object({
      type            = string
      max_price       = optional(string)
      reservation_ids = list(string)
    })
    min_nodes           = number
    max_nodes           = number
    endpoint_floor_gpus = number
    driver_preset       = string
    boot_disk_gib       = number
    local_nvme          = bool
    local_nvme_mode     = string
    interconnect        = string
    infiniband_fabric   = optional(string)
    labels              = map(string)
  }))
  default = {}
}

variable "gpu_operator" {
  description = "GPU node groups without a Managed Kubernetes driver preset: the NVIDIA GPU Operator installs the driver (terraform.tfvars gpu_operator.enabled)."
  type        = bool
  default     = false
}

variable "cpu_pools" {
  description = "CPU-only node groups for batch work without a GPU; the shape of `regions.*.cpu_pools` in terraform.tfvars."
  type = map(object({
    platform      = string
    preset        = string
    min_nodes     = number
    max_nodes     = number
    boot_disk_gib = number
    labels        = map(string)
  }))
  default = {}
}

variable "weights_filesystem" {
  description = "Shared filesystem at /mnt/weights on GPU nodes; optionally on system nodes for CPU snapshot controllers."
  type = object({
    enabled         = bool
    size_gib        = number
    type            = string
    mount_on_system = optional(bool, false)
  })
}

variable "protect_data" {
  description = "Refuse `terraform destroy` of the weights filesystem (prevent_destroy)."
  type        = bool
  default     = true
}

variable "image_cache" {
  description = "Logical registry host and the Zot NodePort; cloud-init writes them into containerd's mirror file on GPU nodes."
  type = object({
    host      = string
    node_port = number
  })
}

variable "public_ip" {
  description = "Allocate a static public IPv4 address for the cluster's gateway load balancer (edge.mode = public)."
  type        = bool
  default     = true
}
