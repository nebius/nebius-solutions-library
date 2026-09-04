#!/bin/bash
# Checkpoint bucket teardown operations, shared by the Terraform destroy
# provisioners (and runnable manually in `empty` mode).
#
# Usage: bucket_teardown.sh <mode> <bucket-name> <endpoint-url>
#
# Modes:
#   guard   - destroy-time preflight, run FIRST in the destroy order (before
#             checkpoint access, Slurm, or storage resources are removed).
#             Without CHECKPOINTS_FORCE_CLEANUP=<bucket-name> in the
#             environment, a non-empty bucket stops the destroy and prints the
#             retain/empty/force options. With it, the current credentials are
#             probed for every capability the later cleanup needs.
#   cleanup - destroy-time bucket emptying, run right before the bucket
#             resource is deleted. Without CHECKPOINTS_FORCE_CLEANUP it only
#             verifies the bucket is stably empty (fail-closed); with it, the
#             bucket contents and incomplete uploads are deleted.
#   empty   - manual invocation: delete all objects AND abort all incomplete
#             multipart uploads, then verify the bucket is empty. This is the
#             "delete the data yourself" teardown option; plain `aws s3 rm`
#             is not enough because it leaves incomplete multipart uploads,
#             which the guard also checks.
#
# CHECKPOINTS_FORCE_CLEANUP is read from the environment at execution time, so
# it can never be stale like state- or plan-captured values, and it lives
# outside Terraform state and saved plans by construction.
set -eu

usage() {
  echo "usage: $0 <guard|cleanup|empty> <bucket-name> <endpoint-url>" >&2
  exit 2
}

[ "$#" -eq 3 ] || usage
mode="$1"
bucket="$2"
endpoint="$3"
case "$mode" in
  guard | cleanup | empty) ;;
  *) usage ;;
esac
force="${CHECKPOINTS_FORCE_CLEANUP:-}"

require_aws() {
  command -v aws >/dev/null || {
    echo "ERROR: $1 requires the aws CLI (Object Storage compatibility client); see the checkpointing README prerequisites." >&2
    exit 1
  }
}

is_empty_count() {
  [ "$1" = "0" ]
}

validate_count() {
  case "$1" in
    *[!0-9]* | "") return 1 ;;
  esac
}

# Each counter prints the count on stdout. A missing bucket prints the literal
# ABSENT; any other failure prints the CLI error on stderr and returns 1.
count_objects() {
  local out
  if ! out=$(aws s3api list-objects-v2 --no-paginate --endpoint-url "$endpoint" --bucket "$bucket" --max-keys 1 --query 'KeyCount' --output text 2>&1); then
    if echo "$out" | grep -q "NoSuchBucket"; then
      echo "ABSENT"
      return 0
    fi
    echo "$out" >&2
    return 1
  fi
  echo "$out"
}

count_uploads() {
  local out
  # shellcheck disable=SC2016 # backticks are JMESPath syntax, not shell
  if ! out=$(aws s3api list-multipart-uploads --no-paginate --endpoint-url "$endpoint" --bucket "$bucket" --max-uploads 1 --query 'length(Uploads || `[]`)' --output text 2>&1); then
    if echo "$out" | grep -q "NoSuchBucket"; then
      echo "ABSENT"
      return 0
    fi
    echo "$out" >&2
    return 1
  fi
  echo "$out"
}

# Sets object_count and multipart_count, exits 0 if the bucket no longer
# exists, exits 1 (with $1 as the refusal context) if inventories cannot be
# read or return something unexpected.
read_counts() {
  object_count=$(count_objects) || {
    echo "ERROR: $1: current credentials cannot list objects in bucket $bucket; refusing to proceed." >&2
    exit 1
  }
  multipart_count=$(count_uploads) || {
    echo "ERROR: $1: current credentials cannot list incomplete uploads in bucket $bucket; refusing to proceed." >&2
    exit 1
  }
  if [ "$object_count" = "ABSENT" ] || [ "$multipart_count" = "ABSENT" ]; then
    echo "Bucket $bucket no longer exists; nothing to do."
    exit 0
  fi
  for count in "$object_count" "$multipart_count"; do
    if ! validate_count "$count"; then
      echo "ERROR: $1: unexpected Object Storage count for bucket $bucket: $count" >&2
      exit 1
    fi
  done
}

# Delete completed objects, abort incomplete multipart uploads, and verify
# both inventories stay empty across two passes. Slurm workloads may still
# have checkpoint uploads in flight for a short tail after cluster teardown.
empty_bucket() {
  local empty_streak=0 abort_failed uploads key upload_id i
  for i in 1 2 3 4 5; do
    aws s3 rm --endpoint-url "$endpoint" "s3://$bucket/" --recursive || true
    abort_failed=false
    if ! uploads=$(aws s3api list-multipart-uploads --endpoint-url "$endpoint" --bucket "$bucket" --query 'Uploads[].[Key,UploadId]' --output text 2>&1); then
      echo "Could not list incomplete uploads on pass $i: $uploads" >&2
      abort_failed=true
      uploads=""
    fi
    if [ -n "$uploads" ] && [ "$uploads" != "None" ]; then
      while IFS=$'\t' read -r key upload_id; do
        if [ -z "$key" ] || [ -z "$upload_id" ] || [ "$upload_id" = "None" ]; then
          echo "Unexpected incomplete-upload inventory row on pass $i" >&2
          abort_failed=true
          continue
        fi
        if ! aws s3api abort-multipart-upload --endpoint-url "$endpoint" --bucket "$bucket" --key "$key" --upload-id "$upload_id" >/dev/null; then
          echo "Could not abort incomplete upload for $key on pass $i" >&2
          abort_failed=true
        fi
      done <<<"$uploads"
    fi
    sleep 5
    object_count=$(count_objects 2>/dev/null) || object_count="?"
    multipart_count=$(count_uploads 2>/dev/null) || multipart_count="?"
    if [ "$object_count" = "ABSENT" ] || [ "$multipart_count" = "ABSENT" ]; then
      echo "Bucket $bucket no longer exists."
      return 0
    fi
    for count in "$object_count" "$multipart_count"; do
      if ! validate_count "$count"; then
        echo "Unexpected Object Storage count on pass $i: $count" >&2
        abort_failed=true
      fi
    done
    if is_empty_count "$object_count" && is_empty_count "$multipart_count" && [ "$abort_failed" = "false" ]; then
      empty_streak=$((empty_streak + 1))
      echo "Bucket $bucket is empty on verification pass $i (stable_checks=$empty_streak/2)"
      if [ "$empty_streak" -ge 2 ]; then
        return 0
      fi
      continue
    fi
    empty_streak=0
    echo "Bucket $bucket still has objects=$object_count incomplete_uploads=$multipart_count after pass $i, retrying (abort_failed=$abort_failed)"
  done
  echo "Failed to empty bucket $bucket - empty it manually and re-run destroy." >&2
  return 1
}

mode_guard() {
  require_aws "the checkpoint destroy guard"
  read_counts "destroy guard"
  if [ "$force" = "$bucket" ]; then
    # Forced cleanup will need working tooling and credentials later in the
    # destroy; verify the exact write/delete capabilities cleanup needs before
    # checkpoint access or Slurm resources are removed. Listing alone can pass
    # for credentials without PutObject/DeleteObject.
    local probe_file probe_key probe_upload_id=""
    probe_file=$(mktemp)
    probe_key=".terraform-checkpoints-cleanup-probe-$RANDOM"
    # shellcheck disable=SC2329 # invoked via the EXIT trap below
    cleanup_probe() {
      rm -f "$probe_file"
      if [ -n "$probe_upload_id" ]; then
        aws s3api abort-multipart-upload --endpoint-url "$endpoint" --bucket "$bucket" --key "$probe_key.multipart" --upload-id "$probe_upload_id" >/dev/null 2>&1 || true
      fi
    }
    trap cleanup_probe EXIT
    : >"$probe_file"
    if ! aws s3api put-object --endpoint-url "$endpoint" --bucket "$bucket" --key "$probe_key" --body "$probe_file" >/dev/null; then
      echo "Refusing to destroy: current credentials cannot write the cleanup permission probe." >&2
      exit 1
    fi
    if ! aws s3api delete-object --endpoint-url "$endpoint" --bucket "$bucket" --key "$probe_key" >/dev/null; then
      echo "Refusing to destroy: current credentials cannot delete objects from bucket $bucket." >&2
      exit 1
    fi
    if ! probe_upload_id=$(aws s3api create-multipart-upload --endpoint-url "$endpoint" --bucket "$bucket" --key "$probe_key.multipart" --query 'UploadId' --output text); then
      echo "Refusing to destroy: current credentials cannot create the incomplete-upload cleanup probe." >&2
      exit 1
    fi
    if [ -z "$probe_upload_id" ] || [ "$probe_upload_id" = "None" ]; then
      echo "Refusing to destroy: Object Storage returned no upload ID for the cleanup probe." >&2
      exit 1
    fi
    if ! aws s3api abort-multipart-upload --endpoint-url "$endpoint" --bucket "$bucket" --key "$probe_key.multipart" --upload-id "$probe_upload_id" >/dev/null; then
      echo "Refusing to destroy: current credentials cannot abort incomplete uploads in bucket $bucket." >&2
      exit 1
    fi
    probe_upload_id=""
    rm -f "$probe_file"
    trap - EXIT
    echo "Cleanup preflight passed for bucket $bucket."
    exit 0
  fi
  if is_empty_count "$object_count" && is_empty_count "$multipart_count"; then
    exit 0
  fi
  {
    echo ""
    echo "Bucket $bucket is not empty. Choose one:"
    echo ""
    echo "  1. Keep the checkpoints, delete everything else:"
    echo "       terraform state rm \\"
    echo "         'module.checkpoints_store[0].nebius_storage_v1_bucket.checkpoints_bucket[0]' \\"
    echo "         'module.checkpoints_store[0].terraform_data.cleanup_bucket[0]' \\"
    echo "         'terraform_data.checkpoint_storage_destroy_guard[0]'"
    echo "       terraform destroy"
    echo ""
    echo "  2. Delete the data yourself (objects AND incomplete uploads), then destroy:"
    echo "       bash '$0' empty '$bucket' '$endpoint'"
    echo "       terraform destroy"
    echo ""
    echo "  3. Force terraform to delete data and bucket:"
    echo "       CHECKPOINTS_FORCE_CLEANUP=$bucket terraform destroy"
  } >&2
  exit 1
}

mode_cleanup() {
  if [ "$force" != "$bucket" ]; then
    echo "CHECKPOINTS_FORCE_CLEANUP is not set for bucket $bucket: leaving its contents untouched."
    # If the bucket is verifiably non-empty, fail HERE: succeeding would remove
    # this helper from state while the bucket deletion still fails, leaving no
    # way to retry the destroy with cleanup enabled.
    require_aws "verifying that bucket $bucket is empty"
    read_counts "cleanup emptiness check"
    if is_empty_count "$object_count" && is_empty_count "$multipart_count"; then
      # The Slurm dependency has been removed, but allow a short window for
      # already-issued writes to become visible. Never remove this helper
      # from state after only one empty snapshot.
      local empty_streak=1 i
      for i in 1 2 3 4; do
        sleep 5
        read_counts "cleanup emptiness recheck (pass $i)"
        if ! is_empty_count "$object_count" || ! is_empty_count "$multipart_count"; then
          echo "Bucket $bucket received checkpoint data during the empty-bucket stability check: refusing to proceed." >&2
          exit 1
        fi
        empty_streak=$((empty_streak + 1))
        if [ "$empty_streak" -ge 3 ]; then
          echo "Bucket $bucket remained empty across three checks"
          exit 0
        fi
      done
      echo "ERROR: bucket $bucket did not reach a stable empty state." >&2
      exit 1
    fi
    {
      echo "Bucket $bucket has checkpoint objects or incomplete uploads: refusing to proceed."
      echo "Keep it (terraform state rm the bucket resources), empty it yourself"
      echo "(bash '$0' empty '$bucket' '$endpoint'), or force"
      echo "cleanup with CHECKPOINTS_FORCE_CLEANUP=$bucket terraform destroy."
    } >&2
    exit 1
  fi
  # Forced cleanup: emptying is required, so missing tooling/credentials is an
  # error - continuing would fail at bucket deletion.
  require_aws "CHECKPOINTS_FORCE_CLEANUP"
  read_counts "forced cleanup"
  empty_bucket
}

mode_empty() {
  require_aws "emptying bucket $bucket"
  read_counts "manual emptying"
  empty_bucket
}

"mode_$mode"
