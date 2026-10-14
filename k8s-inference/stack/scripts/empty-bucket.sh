#!/usr/bin/env bash
# Empty an Object Storage bucket so Terraform can delete it (Nebius refuses to delete a non-empty bucket:
# "BucketNotEmpty"). Run by destroy-time provisioners of the disposable buckets (tenant buckets with
# protect_data = false, the backups bucket). Needs the AWS CLI; credentials in AWS_ACCESS_KEY_ID /
# AWS_SECRET_ACCESS_KEY (the key that owns the bucket, passed by the provisioner).
#
#   stack/scripts/empty-bucket.sh <endpoint> <bucket>
set -euo pipefail
endpoint="${1:?endpoint}"; bucket="${2:?bucket}"
if ! command -v aws > /dev/null; then
  echo "!! empty-bucket: the AWS CLI is needed to empty $bucket before Terraform deletes it (pip install awscli)" >&2
  exit 1
fi
aws --endpoint-url "$endpoint" s3 rm "s3://$bucket" --recursive --quiet || true
left=$(aws --endpoint-url "$endpoint" s3 ls "s3://$bucket" --recursive 2> /dev/null | wc -l)
echo "empty-bucket: $bucket, $left object(s) left"
[ "$left" = 0 ]
