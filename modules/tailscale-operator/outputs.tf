output "namespace" {
  description = "Namespace where the Tailscale operator is installed."
  value       = var.namespace
}

output "helm_release_name" {
  description = "Name of the Tailscale operator Helm release."
  value       = helm_release.this.name
}

output "cilium_bpf_lb_sock_hostns_only_enabled" {
  description = "Whether the Cilium bpf-lb-sock-hostns-only feature flag is enabled."
  value       = var.enable_cilium_bpf_lb_sock_hostns_only
}
