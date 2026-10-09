# =============================================================================
# WireGuard VPN Module
# =============================================================================

# -----------------------------------------------------------------------------
# Public IP Allocation
# -----------------------------------------------------------------------------
resource "nebius_vpc_v1_allocation" "wireguard" {
  parent_id = var.parent_id
  name      = "${var.name_prefix}-wireguard-ip"

  ipv4_public = {
    cidr      = "/32"
    subnet_id = var.subnet_id
  }

  lifecycle {
    create_before_destroy = true
  }
}

# -----------------------------------------------------------------------------
# Boot Disk
# -----------------------------------------------------------------------------
resource "nebius_compute_v1_disk" "wireguard" {
  parent_id           = var.parent_id
  name                = "${var.name_prefix}-wireguard-boot"
  size_bytes          = var.disk_size_gib * 1024 * 1024 * 1024
  block_size_bytes    = 4096
  type                = "NETWORK_SSD"
  source_image_family = { image_family = "ubuntu22.04-driverless" }
}

# -----------------------------------------------------------------------------
# WireGuard Instance
# -----------------------------------------------------------------------------
resource "nebius_compute_v1_instance" "wireguard" {
  parent_id = var.parent_id
  name      = "${var.name_prefix}-wireguard"

  boot_disk = {
    attach_mode   = "READ_WRITE"
    existing_disk = nebius_compute_v1_disk.wireguard
  }

  network_interfaces = [
    {
      name      = "eth0"
      subnet_id = var.subnet_id
      ip_address = {}
      public_ip_address = {
        allocation_id = nebius_vpc_v1_allocation.wireguard.id
      }
    }
  ]

  resources = {
    platform = var.platform
    preset   = var.preset
  }

  cloud_init_user_data = templatefile("${path.module}/templates/cloud-init.yaml", {
    ssh_user_name  = var.ssh_user_name
    ssh_public_key = var.ssh_public_key
    wg_port        = var.wg_port
    wg_network     = var.wg_network
    vpc_cidr       = var.vpc_cidr
    ui_port        = var.ui_port
  })
}
