data "terraform_remote_state" "infra" {
  backend = "local"

  config = {
    path = abspath(var.infra_state_path)
  }
}

ephemeral "nebius_mysterybox_v1_secret_payload_entry" "postgresql_password" {
  count = (
    try(data.terraform_remote_state.infra.outputs.mysterybox_secrets.postgresql_secret_id, null) != null
    && (var.postgres_password == null || var.postgres_password == "")
    && try(data.terraform_remote_state.infra.outputs.postgresql_password, "") == ""
  ) ? 1 : 0

  secret_id = data.terraform_remote_state.infra.outputs.mysterybox_secrets.postgresql_secret_id
  key       = "password"
}

ephemeral "nebius_mysterybox_v1_secret_payload_entry" "storage_secret_access_key" {
  count = (
    try(data.terraform_remote_state.infra.outputs.storage_secret_reference_id, null) != null
    && (var.storage_secret_access_key == null || var.storage_secret_access_key == "")
  ) ? 1 : 0

  secret_id = data.terraform_remote_state.infra.outputs.storage_secret_reference_id
  key       = "secret"
}

resource "random_password" "oidc_client_secret" {
  length  = 32
  special = false
}

resource "random_password" "keycloak_admin_password" {
  length  = 32
  special = false
}

resource "random_password" "keycloak_db_password" {
  length  = 32
  special = false
}

locals {
  infra_outputs                     = try(data.terraform_remote_state.infra.outputs, {})
  infra_postgresql                  = try(local.infra_outputs.postgresql, null)
  infra_storage_bucket              = try(local.infra_outputs.storage_bucket, null)
  infra_storage_credentials         = try(local.infra_outputs.storage_credentials, null)
  infra_storage_secret_reference_id = try(local.infra_outputs.storage_secret_reference_id, null)
  infra_mysterybox_secrets          = try(local.infra_outputs.mysterybox_secrets, null)
  infra_region                      = try(local.infra_outputs.region, "")

  postgres_host = (
    var.postgres_host != null && var.postgres_host != ""
    ? var.postgres_host
    : try(local.infra_postgresql.host, "")
  )
  postgres_port = (
    var.postgres_port != null
    ? var.postgres_port
    : try(local.infra_postgresql.port, 5432)
  )
  postgres_db = (
    var.postgres_db != null && var.postgres_db != ""
    ? var.postgres_db
    : try(local.infra_postgresql.database, "osmo")
  )
  postgres_user = (
    var.postgres_user != null && var.postgres_user != ""
    ? var.postgres_user
    : try(local.infra_postgresql.username, "")
  )
  postgres_password = (
    var.postgres_password != null && var.postgres_password != ""
    ? var.postgres_password
    : try(local.infra_outputs.postgresql_password, "") != ""
    ? local.infra_outputs.postgresql_password
    : try(ephemeral.nebius_mysterybox_v1_secret_payload_entry.postgresql_password[0].data.string_value, "")
  )

  storage_access_key_id = (
    var.storage_access_key_id != null && var.storage_access_key_id != ""
    ? var.storage_access_key_id
    : try(local.infra_storage_credentials.access_key_id, "")
  )
  storage_secret_access_key = (
    var.storage_secret_access_key != null && var.storage_secret_access_key != ""
    ? var.storage_secret_access_key
    : try(ephemeral.nebius_mysterybox_v1_secret_payload_entry.storage_secret_access_key[0].data.string_value, "")
  )
  storage_endpoint = (
    var.storage_endpoint != null && var.storage_endpoint != ""
    ? var.storage_endpoint
    : try(local.infra_storage_bucket.endpoint, "")
  )
  storage_region = (
    var.storage_region != null && var.storage_region != ""
    ? var.storage_region
    : local.infra_region
  )

  oidc_client_secret      = coalesce(var.oidc_client_secret, random_password.oidc_client_secret.result)
  keycloak_admin_password = coalesce(var.keycloak_admin_password, random_password.keycloak_admin_password.result)
  keycloak_db_password    = coalesce(var.keycloak_db_password, random_password.keycloak_db_password.result)
}
