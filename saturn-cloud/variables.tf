############################
# Nebius-injected (infrastructure context)
############################

variable "project_id" {
  description = "The Nebius project ID"
  type        = string
}

variable "subnet_id" {
  description = "The subnet ID where the cluster will be deployed"
  type        = string
}

variable "viewers_group_id" {
  description = "The ID of the viewers group for Nebius Container Registry access"
  type        = string
}

variable "iam_token" {
  description = "IAM token for Kubernetes/Helm provider authentication"
  type        = string
  sensitive   = true
}

variable "region" {
  description = "Nebius region (used for saturn_region and saturn_availability_zone)"
  type        = string
}

############################
# User-provided (marketplace form)
############################

variable "cluster_name" {
  description = "Name of the Kubernetes cluster"
  type        = string
}

variable "saturn_domain" {
  description = "Saturn Cloud domain. Leave empty to let the chart derive it from saturn_customer_name as <customer>.saturnenterprise.io (base_url/ssh_domain follow). Set only for customers on their own domain."
  type        = string
  default     = ""
}

variable "saturn_admin_email" {
  description = "Saturn Cloud admin email"
  type        = string
}

variable "saturn_customer_name" {
  description = "Saturn Cloud customer name"
  type        = string
}

variable "saturn_bootstrap_token" {
  description = "Saturn Cloud bootstrap token"
  type        = string
  sensitive   = true
}

variable "manage_helm" {
  description = "Whether Terraform manages the Saturn Helm release. Set to false to manage Helm externally."
  type        = bool
  default     = true
}

variable "image_overrides" {
  description = "Optional map of chart image overrides (e.g. {saturnEnterprise = \"...:2026.02.01-74\"}). For development; production images are injected at chart-build time."
  type        = map(string)
  default     = {}
}

variable "helm_chart_local_path" {
  description = "If set, install the saturn-helm-operator-nebius chart from this local directory instead of the OCI registry (run `helm dependency build` on it first). For development/iteration."
  type        = string
  default     = ""
}

variable "helm_chart_version" {
  description = "Version of the saturn-helm-operator-nebius chart"
  type        = string
  # Region-aware instanceConfig (saturn-k8s #990), scale-from-zero node affinity on
  # nebius.com labels (#991), and customerName-derived domain + main-namespace
  # creation (#992); published to OCI by release-images #508.
  default = "2026.02.01-123"
}

variable "k8s_version" {
  description = "Kubernetes version for the cluster"
  type        = string
  default     = "1.33"
}

############################
# Shared filesystem (filestore)
############################

variable "enable_filestore" {
  description = "Enable Nebius shared filesystem (filestore) for ReadWriteMany storage."
  type        = bool
  default     = true
}

variable "existing_filestore" {
  description = "ID of an existing filestore to use. If empty, a new one is created when enable_filestore=true."
  type        = string
  default     = ""
}

variable "filestore_disk_type" {
  description = "Filestore disk type."
  type        = string
  default     = "NETWORK_SSD"
}

variable "filestore_disk_size_gibibytes" {
  description = "Filestore disk size in GiB."
  type        = number
  default     = 100
}

variable "filestore_block_size_kibibytes" {
  description = "Filestore block size in KiB."
  type        = number
  default     = 4
}

variable "filestore_mount_path" {
  description = "Mount path for the shared filesystem on Kubernetes nodes."
  type        = string
  default     = "/mnt/data"
}

variable "filesystem_csi" {
  description = "Configuration for the Nebius Shared Filesystem CSI driver."
  type = object({
    chart_version = optional(string, "0.1.6")
    namespace     = optional(string, "kube-system")
  })
  default = {}
}

variable "ssh_public_key" {
  description = "SSH public key for node access (needed for cloud-init when filestore is enabled)."
  type = object({
    key  = optional(string)
    path = optional(string, "~/.ssh/id_rsa.pub")
  })
  default = {}
}

############################
# Node pool configuration
############################

variable "node_pools" {
  description = <<-EOT
    Optional override for the node groups to create. Leave null (default) to derive a
    sensible set from var.region (see region_node_pools in locals.tf), which is kept in
    lock-step with the saturn-helm-operator-nebius chart's per-region instance sizes.
    Set this only to deviate from the region defaults; each pool then becomes a node
    group whose native labels back a chart instance size.
  EOT
  type = list(object({
    platform          = string
    preset            = string
    min_nodes         = optional(number, 0)
    max_nodes         = optional(number, 10)
    boot_disk_gb      = optional(number, 372)
    infiniband_fabric = optional(string)
    drivers_preset    = optional(string)
  }))
  default = null
}
