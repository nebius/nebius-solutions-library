locals {
  test_tspr_host = trimsuffix(nebius_compute_v1_instance.tailscale_peer_relay_instance["0"].status.network_interfaces[0].public_ip_address.address, "/32")
}

resource "null_resource" "check_tailscale_peer_relay_instance" {
  count = var.test_mode ? 1 : 0

  connection {
    user = var.ssh_user_name
    host = local.test_tspr_host
  }

  provisioner "remote-exec" {
    inline = [
      "set -eu",
      "cloud-init status --wait",
      "tailscale status",
      "systemctl -q status tailsacled.service > /dev/null",
      ".nebius/bin/nebius iam whoami > /dev/null"
    ]
  }
}
