# The fleet database: Nebius Managed Service for PostgreSQL (docs.nebius.com/postgresql), one cluster in the
# control plane's project and region (control_plane.database). Two databases: `platform` (model definitions
# and whatever else the platform must remember; owned by the customer API) and `litellm` (API keys, budgets,
# spend). Private access only: the control cluster's nodes reach the private endpoint through the VPC
# network of the control subnet. Backups are the service's (point-in-time, `backup_retention`). The
# `litellm` database is created by the platform stage from the bootstrap connection (stack/platform/database.tf).

data "nebius_vpc_v1_subnet" "control" {
  id = local.f.control_plane.subnet_id
}

resource "random_password" "database" {
  length  = 32
  special = false
}

resource "nebius_msp_postgresql_v1alpha1_cluster" "platform" {
  parent_id   = local.f.control_plane.project_id
  name        = "${local.f.name}-db"
  description = "Serverless 2 fleet ${local.f.name}: platform and litellm databases" # the service allows letters, digits, spaces, _ - , : { } only
  labels      = local.f.labels
  network_id  = data.nebius_vpc_v1_subnet.control.network_id
  bootstrap = {
    db_name       = "platform"
    user_name     = "serverless2"
    user_password = random_password.database.result
  }
  config = {
    version       = "16"
    public_access = false
    pooler_config = { pooling_mode = "SESSION" } # the service's pooler fronts the endpoint; session mode keeps prepared statements (LiteLLM's Prisma, psycopg) working
    template = {
      resources = { platform = local.f.control_plane.database.platform, preset = local.f.control_plane.database.preset }
      disk      = { size_gibibytes = local.f.control_plane.database.disk_gib, type = "network-ssd" }
      hosts     = { count = local.f.control_plane.database.hosts }
    }
  }
  backup = {
    retention_policy    = local.f.control_plane.database.backup_retention
    backup_window_start = local.f.control_plane.database.backup_window_start
  }
}

locals {
  # "<host>:<port>" or "<host>" from the service; the platform stage builds the connection strings.
  database_endpoint = try(nebius_msp_postgresql_v1alpha1_cluster.platform.status.connection_endpoints.private_read_write, "")
  database_host     = split(":", local.database_endpoint)[0]
  database_port     = length(split(":", local.database_endpoint)) > 1 ? tonumber(split(":", local.database_endpoint)[1]) : 5432
}

output "database" {
  description = "The managed PostgreSQL of the fleet: private endpoint, user and databases (the password is in `secrets`)."
  value = {
    id        = nebius_msp_postgresql_v1alpha1_cluster.platform.id
    host      = local.database_host
    port      = local.database_port
    user      = "serverless2"
    databases = { platform = "platform", litellm = "litellm" }
  }
}
