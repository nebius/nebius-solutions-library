resource "tls_private_key" "bastion_sa_key" {
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "nebius_iam_v1_service_account" "bastion-sa" {
  parent_id = var.parent_id
  name      = "bastion-sa"
}

data "nebius_iam_v1_group" "admins-group" {
  name      = "editors"
  parent_id = var.tenant_id
}

resource "nebius_iam_v1_group_membership" "bastion-sa-admin" {
  parent_id = data.nebius_iam_v1_group.admins-group.id
  member_id = nebius_iam_v1_service_account.bastion-sa.id
}

resource "nebius_iam_v1_auth_public_key" "bastion-sa-public-key" {
  parent_id  = var.parent_id
  expires_at = timeadd(timestamp(), "8760h") # 1 Year expiration time
  account = {
    service_account = {
      id = nebius_iam_v1_service_account.bastion-sa.id
    }
  }
  data = tls_private_key.bastion_sa_key.public_key_pem

  lifecycle {
    ignore_changes = [
      expires_at,
    ]
  }
}

locals {
  # these variables don't seem to be in use
  sa_public_key      = tls_private_key.bastion_sa_key.public_key_pem
  sa_private_key     = tls_private_key.bastion_sa_key.private_key_pem
  sa_public_key_id   = nebius_iam_v1_auth_public_key.bastion-sa-public-key.id
  service_account_id = nebius_iam_v1_service_account.bastion-sa.id
}
