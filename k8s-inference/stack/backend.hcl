# Constant part of the s3 backend configuration (Nebius Object Storage); stack.sh adds bucket, key, region and
# endpoints per stage. docs.nebius.com/terraform-provider/store-terraform-state; Terraform >= 1.11 for use_lockfile.
use_lockfile                = true
skip_credentials_validation = true
skip_region_validation      = true
skip_requesting_account_id  = true
skip_metadata_api_check     = true
skip_s3_checksum            = true
