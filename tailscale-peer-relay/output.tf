output "tailscale_peer_relay_instancehost_public_ips" {
  value = {
    for key, instance in nebius_compute_v1_instance.tailscale_peer_relay_instance :
    key => trimsuffix(instance.status.network_interfaces[0].public_ip_address.address, "/32")
  }
}
