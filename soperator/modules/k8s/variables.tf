variable "iam_project_id" {
  description = "ID of the IAM project."
  type        = string
}

variable "vpc_subnet_id" {
  description = "ID of VPC subnet."
  type        = string
}

#---

# K8s cluster 
variable "k8s_version" {
  description = "Kubernetes version to be used in the cluster. Leave null to use backend default (recommended), or choose 1.31 or above."
  type        = string
  default     = null
}

variable "name" {
  description = "Name of the k8s cluster."
  type        = string
}

variable "etcd_cluster_size" {
  description = "Size of the etcd cluster."
  type        = number
  default     = 3
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
  description = "Worker node groups specification."
  type = list(object({
    size                    = number
    max_unavailable_percent = number
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
      infiniband_fabric = string
    }))
    nodeset_index = number
    subset_index  = number
  }))
}

variable "node_group_login" {
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
