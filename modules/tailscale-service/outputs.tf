output "service_name" {
  description = "Name of the Kubernetes Service created for Tailscale exposure."
  value       = kubernetes_service_v1.this.metadata[0].name
}

output "service_namespace" {
  description = "Namespace of the Kubernetes Service created for Tailscale exposure."
  value       = kubernetes_service_v1.this.metadata[0].namespace
}

output "tailnet_hostname" {
  description = "MagicDNS hostname assigned to the Tailscale-backed service."
  value       = local.tailnet_ingress_hostname
}

output "tailnet_endpoints" {
  description = "Map of service port names or numbers to host:port endpoints on the tailnet."
  value = local.tailnet_ingress_hostname == null ? {} : {
    for port in var.ports :
    coalesce(try(port.name, null), tostring(port.port)) => format(
      "%s:%d",
      local.tailnet_ingress_hostname,
      port.port
    )
  }
}
