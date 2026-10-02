# =============================================================================
# WireGuard Module Outputs
# =============================================================================

output "public_ip" {
  description = "WireGuard server public IP"
  value       = nebius_vpc_v1_allocation.wireguard.status.details.allocated_cidr
}

output "private_ip" {
  description = "WireGuard server private IP"
  value       = nebius_compute_v1_instance.wireguard.status.network_interfaces[0].ip_address.address
}

output "ui_url" {
  description = "WireGuard Web UI URL"
  value       = "http://${nebius_vpc_v1_allocation.wireguard.status.details.allocated_cidr}:${var.ui_port}"
}

output "ssh_command" {
  description = "SSH command to connect"
  value       = "ssh ${var.ssh_user_name}@${nebius_vpc_v1_allocation.wireguard.status.details.allocated_cidr}"
}

output "instance_id" {
  description = "WireGuard instance ID"
  value       = nebius_compute_v1_instance.wireguard.id
}
