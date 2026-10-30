ssh_user_name = "ubuntu"
ssh_public_key = {
  key = "<replace with public key>"
  }

tailscale = {
  instance_count            = 1
  instance_preset           = "8vcpu-32gb"
  version                   = "1.94.2"
  relay_server_port         = 40000
  auth_mysterybox_secret_id = "mbsec-e00xxxxx"
}