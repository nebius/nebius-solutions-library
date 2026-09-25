resource "nebius_compute_v1_instance" "tailscale_peer_relay_instance" {
  count     = var.tailscale.instance_count
  parent_id = var.parent_id
  name      = "tailscale-peer-relay-${count.index}"

  boot_disk = {
    attach_mode   = "READ_WRITE"
    existing_disk = nebius_compute_v1_disk.tailscale-peer-relay-boot-disk[count.index]
  }

  network_interfaces = [
    {
      name              = "eth0"
      subnet_id         = var.subnet_id
      ip_address        = {}
      public_ip_address = {}
    }
  ]

  resources = {
    platform = "cpu-d3"
    preset   = var.tailscale.instance_preset
  }

  service_account_id = nebius_iam_v1_service_account.mysterybox-payload-reader-sa.id

  cloud_init_user_data = templatefile("../modules/cloud-init/tailscale-peer-relay-cloud-init.tftpl", {
    ssh_user_name               = var.ssh_user_name
    ssh_public_key              = local.ssh_public_key
    parent_id                   = var.parent_id
    tailscale_version           = var.tailscale.version
    tailscale_relay_server_port = var.tailscale.relay_server_port
    mysterybox_secret_id        = var.tailscale.auth_mysterybox_secret_id
  })
}
