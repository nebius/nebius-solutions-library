resource "nebius_storage_v1_bucket" "backups_bucket" {
  parent_id = var.iam_project_id
  name      = "${var.instance_name}-backups"
}

resource "terraform_data" "cleanup_bucket" {
  count = var.cleanup_bucket_on_destroy ? 1 : 0

  triggers_replace = {
    bucket_name = nebius_storage_v1_bucket.backups_bucket.name
  }

  depends_on = [
    nebius_storage_v1_bucket.backups_bucket
  ]

  provisioner "local-exec" {
    when        = destroy
    working_dir = path.root
    interpreter = ["/bin/bash", "-c"]
    command     = <<-EOT
      set -eu
      command -v aws >/dev/null || { echo "aws cli not found, skipping"; exit 0; }
      bucket="${self.triggers_replace.bucket_name}"
      if ! aws s3api head-bucket --bucket "$bucket" 2>/dev/null; then
        echo "Bucket $bucket doesn't exist, skipping cleanup"
        exit 0
      fi
      # The caller's depends_on guarantees module.slurm finished destroying before we run, but helm uninstall
      # returns as soon as k8s accepts the delete request -- backup pods are still in Terminating state and
      # restic can keep writing for up to terminationGracePeriodSeconds.
      # Loop until list-objects-v2 confirms the bucket is empty so the subsequent bucket delete
      # doesn't race with the termination tail.
      for i in 1 2 3 4 5; do
        aws s3 rm "s3://$bucket/" --recursive || true
        sleep 5
        count=$(aws s3api list-objects-v2 --bucket "$bucket" --query 'KeyCount' --output text 2>/dev/null || echo "?")
        if [ "$count" = "0" ] || [ "$count" = "None" ]; then
          echo "Bucket $bucket emptied on pass $i"
          exit 0
        fi
        echo "Bucket $bucket still has $count objects after pass $i, retrying"
      done
      echo "Bucket $bucket still not empty after 5 passes"
      exit 1
    EOT
  }
}


output "name" {
  value = nebius_storage_v1_bucket.backups_bucket.name
}

output "endpoint" {
  value = "https://${nebius_storage_v1_bucket.backups_bucket.status.domain_name}:443"
}

output "bucket_id" {
  value = nebius_storage_v1_bucket.backups_bucket.id
}
