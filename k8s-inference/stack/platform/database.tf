# The fleet database on the control cluster: connection Secrets for the API and LiteLLM, and the one-time
# creation of the `litellm` database (the managed cluster is bootstrapped with `platform` only; stack/cloud).
# The managed PostgreSQL cluster itself is a cloud-stage resource; its private endpoint is reachable from the
# control cluster's nodes (same VPC network). Backups and failover are the service's.

locals {
  db           = local.cloud.database
  db_password  = local.secrets.database_password
  db_url       = { for name, dbname in local.db.databases : name => "postgresql://${local.db.user}:${urlencode(local.db_password)}@${local.db.host}:${local.db.port}/${dbname}?sslmode=verify-full&sslrootcert=/etc/ssl/certs/ca-certificates.crt" }
  db_namespace = local.role.control ? toset(["api", "litellm"]) : toset([])
}

# One Secret per consumer namespace: host/port/user/password plus ready-made connection strings.
resource "kubernetes_secret_v1" "database" {
  for_each = local.db_namespace
  metadata {
    name      = "database"
    namespace = each.key
  }
  data = {
    host         = local.db.host
    port         = tostring(local.db.port)
    user         = local.db.user
    password     = local.db_password
    platform_url = local.db_url.platform
    litellm_url  = local.db_url.litellm
  }
  depends_on = [kubernetes_namespace_v1.ns]
}

# The LiteLLM chart reads user and password from its own Secret (values `db.secret`).
resource "kubernetes_secret_v1" "litellm_db" {
  count = local.role.control ? 1 : 0
  metadata {
    name      = "litellm-db"
    namespace = "litellm"
  }
  data       = { username = local.db.user, password = local.db_password }
  depends_on = [kubernetes_namespace_v1.ns]
}

# `CREATE DATABASE litellm` once, from the bootstrap database (idempotent; re-runs when the endpoint changes).
resource "kubernetes_job_v1" "database_init" {
  count = local.role.control ? 1 : 0
  metadata {
    name      = "database-init"
    namespace = "litellm"
    labels    = { "serverless2.nebius/endpoint" = md5(local.db.host) }
  }
  spec {
    backoff_limit = 10
    template {
      metadata {}
      spec {
        restart_policy = "OnFailure"
        container {
          name    = "psql"
          image   = "${local.images_host}/docker/library/postgres:16-alpine"
          command = ["sh", "-c"]
          args = [<<-EOT
            set -eu
            for i in $(seq 1 60); do psql "$PLATFORM_URL" -tAc 'select 1' > /dev/null 2>&1 && break; echo "waiting for the database ($i)"; sleep 10; done
            if [ "$(psql "$PLATFORM_URL" -tAc "select 1 from pg_database where datname = 'litellm'")" != "1" ]; then
              psql "$PLATFORM_URL" -c 'create database litellm'
              echo "database litellm created"
            else
              echo "database litellm exists"
            fi
          EOT
          ]
          env {
            name = "PLATFORM_URL"
            value_from {
              secret_key_ref {
                name = "database"
                key  = "platform_url"
              }
            }
          }
          dynamic "volume_mount" {
            for_each = local.trust_mounts
            content {
              name       = volume_mount.value.name
              mount_path = volume_mount.value.mountPath
              sub_path   = volume_mount.value.subPath
              read_only  = true
            }
          }
        }
        dynamic "volume" {
          for_each = local.trust_volumes
          content {
            name = volume.value.name
            config_map { name = volume.value.configMap.name }
          }
        }
      }
    }
  }
  wait_for_completion = true
  timeouts { create = "15m" }
  depends_on = [kubernetes_secret_v1.database, kubernetes_config_map_v1.trust, helm_release.wave2]
}
