variable "parent_id" {
  description = "Nebius project ID."
  type        = string
}

variable "tenant_id" {
  description = "Nebius tenant ID."
  type        = string
}

variable "region" {
  description = "Nebius region."
  type        = string

  validation {
    condition     = contains(["eu-west1", "eu-north1", "eu-north2", "us-central1", "me-west1", "uk-south1", "eu-west2", "us-north1"], var.region)
    error_message = "region must be one of the regions with a defaults entry in locals.tf."
  }
}

variable "subnet_id" {
  description = "VPC subnet ID for the cluster and node groups."
  type        = string
}

variable "iam_token" {
  description = "Nebius IAM token used by the helm/kubernetes providers."
  type        = string
  sensitive   = true
}

variable "node_group_sa_editors" {
  description = "Add the k8s node-group service account to the tenant-wide 'editors' IAM group. Only enable this if image pulls or node operations fail without it: the node SA's credentials are readable from every node, so membership grants any compromised node tenant-wide editor rights. Least-privilege default: false."
  type        = bool
  default     = false
}

variable "cluster_name" {
  description = "Mk8s cluster name. Interpolated into Nebius resource names (cluster, node groups, service account), so it must be a valid lowercase DNS-label-like name."
  type        = string
  default     = "runai"

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])$", var.cluster_name))
    error_message = "cluster_name must be 2-31 chars of lowercase letters, digits and hyphens, starting and ending with a letter or digit (it is interpolated into Nebius resource names)."
  }
}

variable "mk8s_cluster_public_endpoint" {
  description = "Expose the mk8s control plane API endpoint publicly."
  type        = bool
  default     = true
}

variable "mk8s_allowed_cidrs" {
  description = "CIDRs allowed to reach the mk8s API public endpoint (e.g. [\"203.0.113.10/32\"]). Per Nebius semantics, null or an empty list leaves the endpoint open to the world — setting this is strongly recommended. Ignored when mk8s_cluster_public_endpoint = false."
  type        = list(string)
  default     = null
}

variable "etcd_cluster_size" {
  description = "etcd cluster size for the mk8s control plane. Must be 1, 3 or 5."
  type        = number
  default     = 3

  validation {
    condition     = contains([1, 3, 5], var.etcd_cluster_size)
    error_message = "etcd_cluster_size must be 1, 3 or 5."
  }
}

variable "k8s_version" {
  description = "Mk8s cluster and node group Kubernetes version. Must stay within the Kubernetes versions supported by the installed Run:ai version (Run:ai support matrix: 1.34-1.36 for Run:ai 2.26; re-check https://docs.nvidia.com/runai/ before changing). A pinned value prevents the platform from silently auto-updating the cluster outside the supported matrix."
  type        = string
  default     = "1.36"

  validation {
    condition     = contains(["1.34", "1.35", "1.36"], var.k8s_version)
    error_message = "k8s_version must be one of [\"1.34\", \"1.35\", \"1.36\"] — the Kubernetes versions in the Run:ai support matrix for this chart version."
  }
}

variable "ssh_user_name" {
  description = "Username used to connect to the nodes. Must be a safe Linux username (the value is interpolated into cloud-init YAML): lowercase letters, digits, '_' and '-', starting with a letter or '_'."
  type        = string
  default     = "ubuntu"

  validation {
    condition     = can(regex("^[a-z_][a-z0-9_-]{0,31}$", var.ssh_user_name))
    error_message = "ssh_user_name must match ^[a-z_][a-z0-9_-]{0,31}$ — a safe Linux username (the value is interpolated into cloud-init YAML)."
  }
}

variable "ssh_public_key" {
  description = "Public SSH key for node access. Either 'key' (content) or 'path' must be set. 'key' must be a single-line OpenSSH public key — multi-line or non-key content is rejected because the value is interpolated into cloud-init YAML (YAML injection risk)."
  type = object({
    key  = optional(string)
    path = optional(string, "~/.ssh/id_rsa.pub")
  })
  default = {}

  validation {
    condition     = var.ssh_public_key.key != null || try(fileexists(var.ssh_public_key.path), false)
    error_message = "ssh_public_key.key must be set, or ssh_public_key.path must point to an existing file."
  }

  validation {
    condition = var.ssh_public_key.key == null || (
      can(regex("^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)) [A-Za-z0-9+/]+={0,3}$", var.ssh_public_key.key)) &&
      !strcontains(var.ssh_public_key.key, "\n")
    )
    error_message = "ssh_public_key.key must be a single-line OpenSSH public key (ssh-ed25519, ssh-rsa, or ecdsa-sha2-nistp256/384/521 followed by base64 key material, optional trailing '=' padding). Multi-line values and trailing text are rejected to prevent cloud-init YAML injection."
  }
}

variable "cpu_nodes_platform" {
  description = "CPU node group platform. null = region default."
  type        = string
  default     = null
}

variable "cpu_nodes_preset" {
  description = "CPU node group preset. null = region default."
  type        = string
  default     = null
}

variable "cpu_nodes_fixed_count" {
  description = "Number of CPU (system) nodes. Runs the Run:ai control plane workloads."
  type        = number
  default     = 3

  validation {
    condition     = var.cpu_nodes_fixed_count >= 1 && var.cpu_nodes_fixed_count <= 50
    error_message = "cpu_nodes_fixed_count must be between 1 and 50."
  }
}

variable "cpu_nodes_public_ips" {
  description = "Assign public IPv4 addresses to CPU nodes. Public IPs expose node SSH to the internet — prefer false and reach nodes via a bastion. Also note the default Nebius public-IPv4 quota is 3 per region (shared with the HAProxy LB and the mk8s endpoint IP)."
  type        = bool
  default     = false
}

variable "cpu_disk_size" {
  description = "Boot disk size in GiB for CPU nodes. Run:ai control plane requires >= 110 GB per system node."
  type        = number
  default     = 128

  validation {
    condition     = var.cpu_disk_size >= 110
    error_message = "cpu_disk_size must be >= 110 (GiB) — the Run:ai control plane requires at least 110 GB per system node."
  }
}

variable "gpu_nodes_platform" {
  description = "GPU node group platform. null = region default."
  type        = string
  default     = null
}

variable "gpu_nodes_preset" {
  description = "GPU node group preset. null = region default."
  type        = string
  default     = null
}

variable "gpu_nodes_fixed_count" {
  description = "Number of GPU nodes."
  type        = number
  default     = 1

  validation {
    condition     = var.gpu_nodes_fixed_count >= 0 && var.gpu_nodes_fixed_count <= 100
    error_message = "gpu_nodes_fixed_count must be between 0 and 100."
  }
}

variable "gpu_nodes_public_ips" {
  description = "Assign public IPv4 addresses to GPU nodes (which run untrusted user workloads). Public IPs expose node SSH to the internet — prefer false. Also note the default Nebius public-IPv4 quota is 3 per region (shared with the HAProxy LB and the mk8s endpoint IP)."
  type        = bool
  default     = false
}

variable "gpu_disk_size" {
  description = "Boot disk size in GiB for GPU nodes."
  type        = number
  default     = 100
}

variable "mig_strategy" {
  description = "MIG strategy for GPU nodes."
  type        = string
  default     = null

  validation {
    condition     = var.mig_strategy == null || contains(["none", "single", "mixed"], coalesce(var.mig_strategy, "null"))
    error_message = "Invalid MIG strategy '${coalesce(var.mig_strategy, "null")}'. Must be one of ['none', 'single', 'mixed'] or left unset."
  }
}

variable "runai_version" {
  description = "Run:ai helm chart version (control-plane and runai-cluster)."
  type        = string
  default     = "2.26.50"
}

variable "ngc_api_key" {
  description = "NVIDIA NGC API key. Used for helm repo auth ($oauthtoken) and the runai-reg-creds pull secret (nvcr.io)."
  type        = string
  sensitive   = true
}

variable "tls_mode" {
  description = "TLS mode for the Run:ai control plane UI: 'self-signed' (auto-generated cert, sslip.io DNS) or 'byo' (bring your own domain and certificate)."
  type        = string
  default     = "self-signed"

  validation {
    condition     = contains(["self-signed", "byo"], var.tls_mode)
    error_message = "tls_mode must be one of: self-signed, byo."
  }
}

variable "runai_domain" {
  description = "Base domain for the Run:ai UI (UI is served at https://runai.<domain>). Required when tls_mode = \"byo\" (e.g. \"runai.example.com\" base would be \"example.com\"); must be null/unset when tls_mode = \"self-signed\"."
  type        = string
  default     = null

  validation {
    condition     = var.tls_mode != "byo" || (var.runai_domain != null && var.runai_domain != "")
    error_message = "runai_domain must be set when tls_mode = \"byo\"."
  }

  validation {
    condition     = var.tls_mode != "self-signed" || var.runai_domain == null
    error_message = "runai_domain must be null when tls_mode = \"self-signed\" (the domain is derived from the ingress LB IP automatically)."
  }

  validation {
    condition     = var.runai_domain == null || can(regex("^[a-z0-9]([a-z0-9.-]{0,250}[a-z0-9])$", var.runai_domain))
    error_message = "runai_domain must be a lowercase DNS name: letters, digits, dots and hyphens only, at most 252 characters, starting and ending with a letter or digit (no uppercase, underscores, whitespace, or trailing dot)."
  }
}

variable "tls_certificate" {
  description = "PEM-encoded TLS certificate for https://runai.<runai_domain>. Required when tls_mode = \"byo\"."
  type        = string
  sensitive   = true
  default     = null

  validation {
    condition     = var.tls_mode != "byo" || (var.tls_certificate != null && var.tls_certificate != "")
    error_message = "tls_certificate must be set when tls_mode = \"byo\"."
  }

  validation {
    condition     = var.tls_certificate == null || strcontains(var.tls_certificate, "BEGIN CERTIFICATE")
    error_message = "tls_certificate must be PEM-encoded (must contain \"BEGIN CERTIFICATE\"). For a certificate chain, concatenate the leaf and intermediate certificates with the leaf first."
  }
}

variable "tls_private_key" {
  description = "PEM-encoded TLS private key matching tls_certificate. Required when tls_mode = \"byo\"."
  type        = string
  sensitive   = true
  default     = null

  validation {
    condition     = var.tls_mode != "byo" || (var.tls_private_key != null && var.tls_private_key != "")
    error_message = "tls_private_key must be set when tls_mode = \"byo\"."
  }

  validation {
    condition     = var.tls_private_key == null || can(regex("BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY", var.tls_private_key))
    error_message = "tls_private_key must be PEM-encoded (must contain \"BEGIN PRIVATE KEY\", \"BEGIN RSA PRIVATE KEY\", \"BEGIN EC PRIVATE KEY\", or \"BEGIN OPENSSH PRIVATE KEY\")."
  }
}

variable "runai_admin_username" {
  description = "Initial Run:ai admin user (email format)."
  type        = string
  default     = "admin@runai.local"

  validation {
    condition     = can(regex("^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}$", var.runai_admin_username))
    error_message = "runai_admin_username must be a valid email address."
  }
}

variable "runai_admin_password" {
  description = "Initial Run:ai admin password. Run:ai rejects passwords that do not meet this policy; fixing a rejected password requires deleting secret runai-backend-gen-tenants-manager in runai-backend, so it is validated here at plan time instead."
  type        = string
  sensitive   = true

  validation {
    condition = (
      length(var.runai_admin_password) >= 8 &&
      can(regex("[A-Z]", var.runai_admin_password)) &&
      can(regex("[a-z]", var.runai_admin_password)) &&
      can(regex("[0-9]", var.runai_admin_password)) &&
      can(regex("[-!@#$%^&*()_+=]", var.runai_admin_password))
    )
    error_message = "runai_admin_password must be >= 8 characters and include uppercase, lowercase, a digit, and a special character from -!@#$%^&*()_+=."
  }
}

variable "runai_cluster_enabled" {
  description = "Install the runai-cluster chart (phase 2). Requires the controlPlane/cluster values generated by the Run:ai UI onboarding wizard."
  type        = bool
  default     = false
}

variable "runai_control_plane_url" {
  description = "Self-hosted control plane URL from the UI onboarding wizard (e.g. https://runai.example.com). Required when runai_cluster_enabled = true."
  type        = string
  default     = null

  validation {
    condition     = !var.runai_cluster_enabled || trimspace(coalesce(var.runai_control_plane_url, "")) != ""
    error_message = "runai_control_plane_url must be set (non-empty) when runai_cluster_enabled = true."
  }
}

variable "runai_cluster_uid" {
  description = "Cluster UID from the UI onboarding wizard. Required when runai_cluster_enabled = true."
  type        = string
  default     = null

  validation {
    condition     = !var.runai_cluster_enabled || trimspace(coalesce(var.runai_cluster_uid, "")) != ""
    error_message = "runai_cluster_uid must be set (non-empty) when runai_cluster_enabled = true."
  }
}

variable "runai_cluster_url" {
  description = "This cluster's FQDN (cluster.url) from the UI onboarding wizard. Required when runai_cluster_enabled = true."
  type        = string
  default     = null

  validation {
    condition     = !var.runai_cluster_enabled || trimspace(coalesce(var.runai_cluster_url, "")) != ""
    error_message = "runai_cluster_url must be set (non-empty) when runai_cluster_enabled = true."
  }
}

variable "runai_cluster_client_secret" {
  description = "controlPlane.clientSecret from the UI onboarding wizard. Required when runai_cluster_enabled = true."
  type        = string
  sensitive   = true
  default     = null

  validation {
    condition     = !var.runai_cluster_enabled || trimspace(var.runai_cluster_client_secret != null ? var.runai_cluster_client_secret : "") != ""
    error_message = "runai_cluster_client_secret must be set (non-empty) when runai_cluster_enabled = true."
  }
}

variable "haproxy_ingress_version" {
  description = "HAProxy ingress controller helm chart version."
  type        = string
  default     = "1.54.3"
}

variable "helm_timeout" {
  description = "Helm install/upgrade wait timeout in seconds for all helm releases. The Run:ai control plane can take ~15 minutes to become ready, so 900s left zero headroom (ADV-17)."
  type        = number
  default     = 2400

  validation {
    condition     = var.helm_timeout >= 600
    error_message = "helm_timeout must be >= 600 seconds — the Run:ai control plane routinely takes ~15 minutes to become ready."
  }
}
