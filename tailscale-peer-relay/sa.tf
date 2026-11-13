resource "nebius_iam_v1_service_account" "mysterybox-payload-reader-sa" {
  parent_id = var.parent_id
  name      = "mysterybox-payload-reader-sa"
}

data "nebius_iam_v1_group" "mysterybox-payload-reader-group" {
  name      = "mysterybox-payload-viewer"
  parent_id = var.tenant_id
}

resource "nebius_iam_v1_group_membership" "mysterybox-sa-binding" {
  parent_id = data.nebius_iam_v1_group.mysterybox-payload-reader-group.id
  member_id = nebius_iam_v1_service_account.mysterybox-payload-reader-sa.id
}
