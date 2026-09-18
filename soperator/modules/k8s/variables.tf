variable "iam_project_id" {
  description = "ID of the IAM project."
  type        = string
}

variable "vpc_subnet_id" {
  description = "ID of VPC subnet."
  type        = string
}

variable "login_public_ip" {
  description = "Public or private ip for login node load balancer"
  type        = bool
  default     = true
}

#---

# K8s cluster
variable "k8s_version" {
  description = "Kubernetes version to be used in the cluster. Leave null to use backend default (recommended), or choose 1.31 or above."
  type        = string
  default     = null
}

variable "node_group_version" {
  description = "Nebius version of node group. Contains bundle of different component versions, e.g. driver, linux_kernel, doca, etc."
  type        = string
}

variable "name" {
  description = "Name of the k8s cluster."
  type        = string
}

variable "company_name" {
  description = "Name of the company. It is used for context name of the cluster in .kubeconfig file."
  type        = string

  validation {
    condition     = var.company_name != ""
    error_message = "Company name is not provided"
  }
}

#---

variable "platform_driver_presets" {
  description = "Per-platform presets for GPU driver. Keys are platform IDs (e.g., gpu-h100-sxm); values are driver presets (e.g., cuda13.0)."
  type        = map(string)
}

#---

variable "node_group_system" {
  description = "System node group specification."
  type = object({
    min_size = number
    max_size = number
    resource = object({
      platform = string
      preset   = string
    })
    boot_disk = object({
      type                 = string
      size_gibibytes       = number
      block_size_kibibytes = number
    })
  })
}

variable "node_group_nfs" {
  description = "NFS node group specification."
  type = object({
    enabled = bool
    spec = optional(object({
      size = number
      resource = object({
        platform = string
        preset   = string
      })
      boot_disk = object({
        type                 = string
        size_gibibytes       = number
        block_size_kibibytes = number
      })
    }))
  })
}

variable "node_group_controller" {
  description = "Controller node group specification."
  type = object({
    size = number
    resource = object({
      platform = string
      preset   = string
    })
    boot_disk = object({
      type                 = string
      size_gibibytes       = number
      block_size_kibibytes = number
    })
  })
}

variable "node_group_workers" {
  description = "Worker node groups specification (legacy)."
  type = list(object({
    size                    = number
    max_unavailable_percent = number
    max_surge_percent       = optional(number)
    drain_timeout           = optional(string)
    resource = object({
      platform = string
      preset   = string
    })
    boot_disk = object({
      type                 = string
      size_gibibytes       = number
      block_size_kibibytes = number
    })
    gpu_cluster = optional(object({
      id                = optional(string)
      infiniband_fabric = optional(string)
    }))
    preemptible   = optional(object({}))
    nodeset_index = number
    subset_index  = number
  }))
}

variable "node_group_workers_v2" {
  description = "Worker node groups specification for nodesets (v2)."
  type = list(object({
    name            = string
    node_group_name = optional(string)
    size            = number
    min_size        = number
    max_size        = number
    autoscaling     = bool
    resource = object({
      platform = string
      preset   = string
    })
    boot_disk = object({
      type                 = string
      size_gibibytes       = number
      block_size_kibibytes = number
    })
    gpu_cluster = optional(object({
      id                = optional(string)
      infiniband_fabric = optional(string)
    }))
    preemptible = optional(object({}))
    reservation_policy = optional(object({
      policy          = optional(string)
      reservation_ids = optional(list(string))
    }))
    nvl_instance_group_id = optional(string)
    # Additional labels applied to the mk8s worker node template.
    extra_labels           = optional(map(string), {})
    max_pods               = optional(number, 32)
    placement_policy_nodes = optional(list(string))
    local_nvme = optional(object({
      enabled                   = optional(bool, false)
      device_count              = optional(number)
      device_capacity_gigabytes = optional(number)
      mount_path                = optional(string, "/mnt/local-nvme")
      size_limit_gibibytes      = optional(number)
    }), {})
    nodeset_index = number
    subset_index  = number
  }))
  default = []

  validation {
    condition = alltrue([
      for worker in var.node_group_workers_v2 :
      worker.max_pods > 0
    ])
    error_message = "Worker node group max_pods must be greater than 0."
  }
}

variable "node_group_login" {
  description = "Login node group specification."
  type = object({
    size               = number
    node_group_enabled = optional(bool, true)
    resource = object({
      platform = string
      preset   = string
    })
    boot_disk = object({
      type                 = string
      size_gibibytes       = number
      block_size_kibibytes = number
    })
  })
}

variable "node_group_accounting" {
  description = "System node group specification."
  type = object({
    enabled = bool
    spec = optional(object({
      resource = object({
        platform = string
        preset   = string
      })
      boot_disk = object({
        type                 = string
        size_gibibytes       = number
        block_size_kibibytes = number
      })
    }))
  })

  validation {
    condition = (var.node_group_accounting.enabled
      ? var.node_group_accounting.spec != null
      : true
    )
    error_message = "Specification must be provided when accounting is enabled."
  }
}

#---

variable "filestores" {
  type = object({
    controller_spool = object({
      id        = string
      mount_tag = string
    })
    jail = object({
      id        = string
      mount_tag = string
    })
    jail_submounts = list(object({
      id        = string
      mount_tag = string
    }))
    accounting = optional(object({
      id        = string
      mount_tag = string
    }))
  })
}

#---

variable "node_ssh_access_users" {
  description = "SSH user credentials for accessing k8s nodes."
  type = list(object({
    name        = string
    public_keys = list(string)
  }))
  default = []
}

variable "node_ssh_access_public_ip" {
  description = "Assign public IP addresses to k8s nodes when node_ssh_access_users is configured."
  type        = bool
  default     = false
}

variable "nvidia_config_lines" {
  description = "Lines to write to /etc/modprobe.d/nvidia_config.conf via cloud-init (GPU workers only)."
  type        = list(string)
  default     = []
}

variable "use_preinstalled_gpu_drivers" {
  description = "Enable preinstalled mode for worker nodes."
  type        = bool
  default     = false
}

variable "use_default_apparmor_profile" {
  description = "Load the soperator-default AppArmor profile on every node at each boot. Requires AppArmor and apparmor_parser in the node image."
  type        = bool
  default     = true
}
