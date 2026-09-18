locals {
  name = { for f in var.fs :
    f.name => join("-", [
      trimsuffix(
        substr(
          var.k8s_cluster_name,
          0,
          64 - (length(f.name) + 1)
        ),
        "-"
      ),
      f.name
    ])
  }
}

resource "nebius_compute_v1_filesystem" "this" {
  for_each = tomap({ for f in var.fs :
    f.name => {
      name            = local.name[f.name]
      storage         = provider::units::from_gib(f.size_gibibytes)
      forbid_deletion = f.forbid_deletion
    }
  })

  parent_id = var.iam_project_id

  name = each.value.name

  type       = "WEKA"
  size_bytes = each.value.storage

  // External filesystems should have block_size_bytes == 0.
  // However, block_size_bytes == 0 forces TF to replace the resource during import
  block_size_bytes = null

  forbid_deletion = each.value.forbid_deletion

  lifecycle {
    ignore_changes = [
      labels,
    ]
  }
}
