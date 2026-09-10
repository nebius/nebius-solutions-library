# Service account for Karpenter to manage Nebius Cloud resources
resource "nebius_iam_v1_service_account" "node-sa" {
  name        = local.name_prefix
  parent_id   = data.nebius_iam_v1_project.project.id
  description = "${local.name_prefix}-${terraform.workspace} service account"
}

# We create a group because access permits can only be granted to groups
resource "nebius_iam_v1_group" "karpenter-manager" {
  name      = "${data.nebius_iam_v1_project.project.id}-${local.name_prefix}-karpenter-manager"
  parent_id = data.nebius_iam_v1_project.project.parent_id
}

# Grant project admin access to the project for the karpenter-manager group
resource "nebius_iam_v1_access_permit" "karpenter-manager-project-admin" {
  parent_id   = nebius_iam_v1_group.karpenter-manager.id
  resource_id = data.nebius_iam_v1_project.project.id
  role        = "admin"
}

# Grant project viewer access to the tenant for the karpenter-manager group
resource "nebius_iam_v1_access_permit" "karpenter-manager-tenant-viewer" {
  parent_id   = nebius_iam_v1_group.karpenter-manager.id
  resource_id = data.nebius_iam_v1_project.project.parent_id
  role        = "viewer"
}

# Add service account to the group
resource "nebius_iam_v1_group_membership" "node-sa-karpenter-manager-membership" {
  parent_id = nebius_iam_v1_group.karpenter-manager.id
  member_id = nebius_iam_v1_service_account.node-sa.id
}

ephemeral "nebius_iam_token" "token" {}
