
resource "terraform_data" "disk_cleanup" {
  triggers_replace = {
    parent_id = var.iam_project_id
  }

  # Policy updates must not replace this resource and run cleanup during apply.
  input = {
    expected_cluster_id = var.expected_cluster_id
    disk_ids            = sort(tolist(var.disk_ids))
  }

  provisioner "local-exec" {
    when        = destroy
    interpreter = ["/bin/bash", "-c"]

    environment = {
      "PARENT_ID" : self.triggers_replace.parent_id,
      # Older state has no input. Its destroy must preserve all disks.
      "EXPECTED_CLUSTER_ID" : try(self.input.expected_cluster_id, ""),
      "DISK_IDS" : jsonencode(try(self.input.disk_ids, [])),
    }
    command = "python3 ${path.module}/scripts/disk_cleanup.py"
  }
}
