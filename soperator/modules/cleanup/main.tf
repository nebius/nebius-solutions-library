
resource "terraform_data" "disk_cleanup" {
  triggers_replace = {
    parent_id = var.iam_project_id
  }

  provisioner "local-exec" {
    when        = destroy
    interpreter = ["/bin/bash", "-c"]

    environment = {
      "PARENT_ID" : self.triggers_replace.parent_id,
    }
    command = "python3 ${path.module}/scripts/disk_cleanup.py"
  }
}
