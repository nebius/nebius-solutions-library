# Module `tenant-region`

The cloud side of one tenant in one region: a service account and group with `storage.object-editor`
on the tenant's bucket (bucket policy), the bucket itself with a lifecycle rule that expires run
outputs, and the S3 access key the tenant namespace uses (Secrets `tenant-storage` and `s3`).

Used once per tenant and worker cluster by `stack/models/main.tf`. With `protect_data = true` the bucket
refuses `terraform destroy`.

## Inputs

| Name | Description | Type | Default |
|---|---|---|---|
| `name` | Bucket, service account and group name (`<fleet>-<tenant>-<region>`) | `string` | required |
| `tenant` | Tenant name | `string` | required |
| `region` | Region of the bucket | `string` | required |
| `project_id` | Project of that region | `string` | required |
| `lifecycle_days` | Days after which run outputs expire (0 = keep) | `number` | required |
| `protect_data` | `prevent_destroy` on the bucket | `bool` | required |
| `labels` | Labels on every resource | `map(string)` | `{}` |

## Outputs

| Name | Description |
|---|---|
| `storage` | Bucket, endpoint, region and the access key pair (sensitive) |
| `service_account_id` | The tenant's service account |
| `bucket` | The bucket name |
