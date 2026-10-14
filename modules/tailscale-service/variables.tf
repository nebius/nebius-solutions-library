variable "namespace" {
  description = "Namespace where the exposed Kubernetes Service should be created."
  type        = string
}

variable "name" {
  description = "Name of the Kubernetes Service created for Tailscale exposure."
  type        = string
}

variable "tailnet_hostname" {
  description = "Hostname advertised by Tailscale for this service."
  type        = string
}

variable "selector" {
  description = "Pod selector used by the Kubernetes Service."
  type        = map(string)
}

variable "ports" {
  description = "Ports exposed through the Tailscale-backed LoadBalancer Service."
  type = list(object({
    name         = optional(string)
    port         = number
    target_port  = number
    protocol     = optional(string, "TCP")
    app_protocol = optional(string)
  }))

  validation {
    condition     = length(var.ports) > 0
    error_message = "At least one port must be defined for a Tailscale service."
  }
}

variable "tags" {
  description = "Tailscale tags applied to the exposed service."
  type        = list(string)
  default     = ["tag:k8s"]
}

variable "additional_annotations" {
  description = "Additional annotations to add to the Kubernetes Service metadata."
  type        = map(string)
  default     = {}
}

variable "load_balancer_class" {
  description = "Load balancer class to use for the Kubernetes Service."
  type        = string
  default     = "tailscale"
}

variable "type" {
  description = "Kubernetes Service type. Leave as LoadBalancer for Tailscale exposure."
  type        = string
  default     = "LoadBalancer"

  validation {
    condition     = var.type == "LoadBalancer"
    error_message = "Tailscale service exposure requires type to be LoadBalancer."
  }
}

variable "operator_namespace" {
  description = "Namespace where the Tailscale operator creates its managed proxy pods."
  type        = string
  default     = "tailscale"
}

variable "restart_generated_proxy_once_after_create" {
  description = "Opt-in workaround for stale Tailscale ingress proxy state. When true, restart the operator-generated proxy once after service creation or replacement."
  type        = bool
  default     = false
}

variable "restart_generated_proxy_strategy" {
  description = "Strategy to use for the opt-in proxy restart workaround. template_annotations patches the generated StatefulSet template annotations through the Kubernetes provider. local_exec deletes the generated proxy pod and waits for readiness."
  type        = string
  default     = "template_annotations"

  validation {
    condition     = contains(["local_exec", "template_annotations"], var.restart_generated_proxy_strategy)
    error_message = "restart_generated_proxy_strategy must be local_exec or template_annotations."
  }
}

variable "kubectl_context" {
  description = "Kubectl context to use for the opt-in proxy restart workaround when restart_generated_proxy_strategy is local_exec."
  type        = string
  default     = null
  nullable    = true
}

variable "proxy_restart_timeout_seconds" {
  description = "Timeout, in seconds, for waiting on the generated Tailscale proxy pod when the opt-in restart workaround is enabled."
  type        = number
  default     = 300

  validation {
    condition     = var.proxy_restart_timeout_seconds > 0
    error_message = "proxy_restart_timeout_seconds must be greater than zero."
  }
}
