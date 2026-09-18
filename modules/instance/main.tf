resource "nebius_compute_v1_disk" "boot-disk" {
  parent_id           = var.parent_id
  name                = join("-", ["instance-boot-disk", var.instance_name])
  block_size_bytes    = 4096
  size_bytes          = 1024 * 1024 * 1024 * var.boot_disk_size_gb
  type                = "NETWORK_SSD"
  source_image_family = { image_family = "ubuntu24.04-cuda12" }
}

resource "nebius_compute_v1_disk" "extra-storage-disk" {
  count            = var.add_extra_storage ? 1 : 0
  parent_id        = var.parent_id
  name             = join("-", ["extra-storage-disk", var.instance_name])
  block_size_bytes = 4096
  size_bytes       = 1024 * 1024 * 1024 * var.extra_storage_size_gb
  type             = var.extra_storage_class
}


resource "nebius_compute_v1_instance" "instance" {
  parent_id = var.parent_id
  name      = var.instance_name

  network_interfaces = [
    {
      name              = "eth0"
      subnet_id         = var.subnet_id
      ip_address        = {}
      public_ip_address = var.public_ip ? {} : null
    }
  ]

  resources = {
    platform = var.platform
    preset   = var.preset
  }

  boot_disk = {
    attach_mode   = "READ_WRITE"
    existing_disk = nebius_compute_v1_disk.boot-disk
  }

  preemptible = var.preemptible ? {
    on_preemption = "STOP"
    priority      = 3
  } : null

  recovery_policy = var.preemptible ? "FAIL" : "RECOVER"



  gpu_cluster = var.gpu_cluster != "" ? { id = var.gpu_cluster } : {}
  secondary_disks = var.add_extra_storage ? [
    {
      attach_mode = "READ_WRITE"
      existing_disk = {
        id = nebius_compute_v1_disk.extra-storage-disk[0].id
      }
    }
  ] : []

  filesystems = var.shared_filesystem_id != "" ? [
    {
      attach_mode = "READ_WRITE"
      existing_filesystem = {
        id = var.shared_filesystem_id
      }
      mount_tag = "filesystem-0"
    }
  ] : []


  cloud_init_user_data = templatefile("../modules/cloud-init/simple-setup-init.tftpl", {
    users                   = local.users,
    extra_path              = local.extra_path,
    extra_disk_id           = local.extra_disk_id,
    shared_filesystem_id    = var.shared_filesystem_id,
    shared_filesystem_mount = var.shared_filesystem_mount,
    aws_access_key_id       = var.aws_access_key_id,
    aws_secret_access_key   = var.aws_secret_access_key,
    mount_bucket            = var.mount_bucket,
    s3_mount_path           = var.s3_mount_path
  })
}

resource "local_file" "cloud_init_variables_log" {
  content  = local.cloud_init_log
  filename = "${path.module}/cloud-init-variables.log"


}
