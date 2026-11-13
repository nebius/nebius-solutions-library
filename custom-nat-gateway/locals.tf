locals {
  ssh_public_key = var.ssh_public_key.key != null ? var.ssh_public_key.key : (
    fileexists(pathexpand(var.ssh_public_key.path)) ? file(pathexpand(var.ssh_public_key.path)) : null
  )
}
