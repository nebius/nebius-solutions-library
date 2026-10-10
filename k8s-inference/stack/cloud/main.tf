# Stage 1, cloud: every Nebius resource of the fleet in ONE state (stack.sh: `cloud`). Clusters and node
# pools (stack/modules/cluster), the per-project operations identity, the registry and backups bucket in
# the hub project, and the random secrets the platform stage installs. Nothing in-cluster happens here.

module "cluster" {
  for_each                    = local.clusters
  source                      = "../modules/cluster"
  name                        = "${local.f.name}-${each.key}"
  project_id                  = each.value.project_id
  subnet_id                   = each.value.subnet_id
  kubernetes_version          = local.f.kubernetes_version
  labels                      = merge(local.f.labels, { "serverless2.nebius/fleet" = local.f.name })
  control_plane_allowed_cidrs = each.value.allowed_cidrs
  system_pool                 = each.value.system_pool
  gpu_pools                   = each.value.pools
  gpu_operator                = local.f.gpu_operator.enabled
  cpu_pools                   = try(each.value.cpu_pools, {})
  weights_filesystem          = each.value.weights_filesystem
  protect_data                = local.f.protect_data
  image_cache                 = { host = local.images_host, node_port = local.f.images.cache.node_port }
  public_ip                   = local.f.edge.mode == "public"
}

# ---------------------------------------------------------------------------
# Operations identity, one per project: the ops CronJobs (stopped spot node recovery), the price feed, the
# cost export and the image cache's registry credential run as it. `editor` on the project is enough.
resource "nebius_iam_v1_service_account" "ops" {
  for_each    = toset(local.projects)
  parent_id   = each.key
  name        = "${local.f.name}-ops"
  description = "Serverless 2.0 fleet ${local.f.name}: operations (node recovery, prices, backups, image cache)"
  labels      = local.f.labels
}

resource "nebius_iam_v1_group" "ops" {
  for_each  = toset(local.projects)
  parent_id = each.key
  name      = "${local.f.name}-ops"
}

resource "nebius_iam_v1_group_membership" "ops" {
  for_each  = toset(local.projects)
  parent_id = nebius_iam_v1_group.ops[each.key].id
  member_id = nebius_iam_v1_service_account.ops[each.key].id
}

resource "nebius_iam_v1_access_permit" "ops_editor" {
  for_each    = toset(local.projects)
  parent_id   = nebius_iam_v1_group.ops[each.key].id
  resource_id = each.key
  role        = "editor"
}

# Auth key of each ops SA: the private half becomes the service-account credentials file the ops image and
# the price feed use (Secret ops/nebius-sa on the clusters; never in git, only in this state).
resource "tls_private_key" "ops" {
  for_each  = toset(local.projects)
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "nebius_iam_v1_auth_public_key" "ops" {
  for_each    = toset(local.projects)
  parent_id   = each.key
  name        = "${local.f.name}-ops-terraform"
  description = "Terraform-issued key of ${local.f.name}-ops"
  account     = { service_account = { id = nebius_iam_v1_service_account.ops[each.key].id } }
  data        = tls_private_key.ops[each.key].public_key_pem
}

locals {
  ops_credentials = { for p in local.projects : p => jsonencode({
    "subject-credentials" = {
      type          = "JWT"
      alg           = "RS256"
      "private-key" = tls_private_key.ops[p].private_key_pem
      kid           = nebius_iam_v1_auth_public_key.ops[p].id
      iss           = nebius_iam_v1_service_account.ops[p].id
      sub           = nebius_iam_v1_service_account.ops[p].id
    }
  }) }
}

# ---------------------------------------------------------------------------
# Hub project: the registry the platform images are pushed to (images.source default) and the reports
# bucket (daily cost reports; named `backups` for history) with its policy for the ops group.
resource "nebius_registry_v1_registry" "images" {
  count     = local.f.images.source == null ? 1 : 0
  parent_id = local.hub_project
  name      = "${local.f.name}-images"
  labels    = local.f.labels
}

# The registry refuses deletion while it holds images: every image object is removed first at destroy time
# through the Registry service's image API (stack/scripts/empty-registry.sh; deleting tags alone leaves the
# manifests behind). Depends on the registry, so `destroy` runs it right before the registry is deleted.
resource "terraform_data" "empty_registry" {
  count = local.f.images.source == null ? 1 : 0
  input = {
    script   = "${path.module}/../scripts/empty-registry.sh"
    registry = nebius_registry_v1_registry.images[0].id
  }
  provisioner "local-exec" {
    when    = destroy
    command = "${self.input.script} ${self.input.registry}"
  }
}

resource "nebius_storage_v1_bucket" "backups" {
  parent_id         = local.hub_project
  name              = "${local.f.name}-backups"
  labels            = local.f.labels
  versioning_policy = "DISABLED"
  bucket_policy = {
    rules = [{ paths = ["*"], roles = ["storage.object-editor"], group_id = nebius_iam_v1_group.ops[local.hub_project].id }]
  }
}

# The backups bucket is emptied before Terraform deletes it (Nebius refuses to delete a non-empty bucket);
# depends on the bucket and the key, so `destroy` runs it first. Needs the AWS CLI on the operator machine.
resource "nebius_storage_v1_bucket" "logs" {
  for_each          = local.clusters
  parent_id         = local.hub_project
  name              = "${local.f.name}-logs-${each.key}"
  labels            = local.f.labels
  versioning_policy = "DISABLED"
  bucket_policy = {
    rules = [{ paths = ["*"], roles = ["storage.object-editor"], group_id = nebius_iam_v1_group.ops[local.hub_project].id }]
  }
}

resource "terraform_data" "empty_logs" {
  for_each = nebius_storage_v1_bucket.logs
  input = {
    script   = "${path.module}/../scripts/empty-bucket.sh"
    endpoint = "https://storage.${local.hub_region}.nebius.cloud"
    bucket   = each.value.name
    key      = nebius_iam_v2_access_key.backups.status.aws_access_key_id
    secret   = nebius_iam_v2_access_key.backups.status.secret
  }
  # the key's access comes from the ops group (bucket policy): `destroy` empties the bucket before the
  # membership goes (seen 2026-10-09: 403 from the provisioner after the membership was destroyed first)
  depends_on = [nebius_iam_v1_group_membership.ops]
  provisioner "local-exec" {
    when    = destroy
    command = "${self.input.script} ${self.input.endpoint} ${self.input.bucket}"
    environment = {
      AWS_ACCESS_KEY_ID     = self.input.key
      AWS_SECRET_ACCESS_KEY = self.input.secret
    }
  }
}

resource "terraform_data" "empty_backups" {
  input = {
    script   = "${path.module}/../scripts/empty-bucket.sh"
    endpoint = "https://storage.${local.hub_region}.nebius.cloud"
    bucket   = nebius_storage_v1_bucket.backups.name
    key      = nebius_iam_v2_access_key.backups.status.aws_access_key_id
    secret   = nebius_iam_v2_access_key.backups.status.secret
  }
  # the key's access comes from the ops group (bucket policy): `destroy` empties the bucket before the
  # membership goes (seen 2026-10-09: 403 from the provisioner after the membership was destroyed first)
  depends_on = [nebius_iam_v1_group_membership.ops]
  provisioner "local-exec" {
    when    = destroy
    command = "${self.input.script} ${self.input.endpoint} ${self.input.bucket}"
    environment = {
      AWS_ACCESS_KEY_ID     = self.input.key
      AWS_SECRET_ACCESS_KEY = self.input.secret
    }
  }
}

# S3 access key of the hub ops SA: the cost export writes with it.
resource "nebius_iam_v2_access_key" "backups" {
  parent_id            = local.hub_project
  name                 = "${local.f.name}-backups"
  description          = "Serverless 2.0 fleet ${local.f.name}: backups and cost reports"
  secret_delivery_mode = "INLINE"
  account              = { service_account = { id = nebius_iam_v1_service_account.ops[local.hub_project].id } }
}

# ---------------------------------------------------------------------------
# Secrets generated once, installed by the platform stage.
resource "random_password" "litellm_master" {
  length  = 48
  special = false
}
resource "random_password" "grafana_admin" {
  length  = 24
  special = false
}

locals {
  images_source = coalesce(local.f.images.source,
  local.f.images.source == null ? "cr.${local.hub_region}.nebius.cloud/${trimprefix(nebius_registry_v1_registry.images[0].id, "registry-")}" : "")
}
