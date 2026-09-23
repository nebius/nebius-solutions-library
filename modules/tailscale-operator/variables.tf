variable "namespace" {
  description = "Namespace where the Tailscale operator will be installed."
  type        = string
  default     = "tailscale"
}

variable "create_namespace" {
  description = "Whether to create the namespace used by the Tailscale operator."
  type        = bool
  default     = true
}

variable "operator_name" {
  description = "Helm release name for the Tailscale operator."
  type        = string
  default     = "tailscale-operator"
}

variable "operator_version" {
  description = "Tailscale Kubernetes operator Helm chart version."
  type        = string
  default     = "1.94.2"
}

variable "operator_hostname" {
  description = "Optional hostname for the operator node on the tailnet. Set to null to let Tailscale assign one."
  type        = string
  default     = null
}

variable "default_tags" {
  description = "Default Tailscale tags applied by the operator to exposed resources."
  type        = list(string)
  default     = ["tag:k8s"]
}

variable "oauth_client_id" {
  description = "Tailscale OAuth client ID for the Kubernetes operator. Set this only when you are not using oauth_secret_name."
  type        = string
  default     = null
  nullable    = true
  sensitive   = true
}

variable "oauth_client_secret" {
  description = "Tailscale OAuth client secret for the Kubernetes operator. Set this only when you are not using oauth_secret_name."
  type        = string
  default     = null
  nullable    = true
  sensitive   = true
}

variable "oauth_secret_name" {
  description = "Name of an existing Kubernetes Secret in the operator namespace that contains client_id and client_secret keys for the operator OAuth credentials."
  type        = string
  default     = null
  nullable    = true
}

variable "enable_cilium_bpf_lb_sock_hostns_only" {
  description = "Whether to set the Cilium bpf-lb-sock-hostns-only feature flag to true."
  type        = bool
  default     = true
}

variable "restart_cilium_after_config_change" {
  description = "Whether to restart the Cilium DaemonSet after applying the compatibility workaround."
  type        = bool
  default     = true
}

variable "cilium_namespace" {
  description = "Namespace where the Cilium DaemonSet and ConfigMap are deployed."
  type        = string
  default     = "kube-system"
}

variable "cilium_config_map_name" {
  description = "Name of the ConfigMap that stores Cilium agent configuration."
  type        = string
  default     = "cilium-config"
}

variable "cilium_daemonset_name" {
  description = "Name of the Cilium DaemonSet to restart when the workaround is enabled."
  type        = string
  default     = "cilium"
}
