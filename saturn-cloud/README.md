# Nebius Kubernetes Cluster for Saturn Cloud

This Terraform configuration creates a Nebius Managed Kubernetes cluster configured for Saturn Cloud Enterprise, leveraging Nebius GPU infrastructure with support for NVIDIA H100, H200, and GB200 GPUs with InfiniBand networking.

## Prerequisites

1. A Nebius account with appropriate permissions
2. Terraform installed (>= 1.0)
3. [Nebius CLI](https://docs.nebius.com/cli/install) installed and configured
4. `jq` installed

## Setup Instructions

### 1. Register Your Saturn Cloud Tenancy

Register your organization via the [Saturn Cloud Enterprise on Nebius](https://saturncloud.io/docs/enterprise/nebius/) guide, or directly:

```bash
curl -X POST https://manager.saturnenterprise.io/api/v2/customers/register \
  -H "Content-Type: application/json" \
  -d '{
    "name": "your-organization-name",
    "email": "your-email@example.com",
    "cloud": "nebius"
  }'
```

You'll receive an activation email.

### 2. Activate Your Account

Click the activation link in your email, or activate via the API:

```bash
curl -X POST https://manager.saturnenterprise.io/v2/activate \
  -H "Content-Type: application/json" \
  -d '{"token": "YOUR_ACTIVATION_TOKEN"}'
```

After activation, you'll receive a sample `terraform.tfvars` pre-filled with your Saturn Cloud configuration (including a bootstrap token). Use it to replace the default `terraform.tfvars` in this directory.

The bootstrap token expires after 4 hours. To regenerate:

```bash
curl -X POST https://manager.saturnenterprise.io/v2/resend-setup \
  -H "Content-Type: application/json" \
  -d '{
    "name": "your-organization-name",
    "email": "your-email@example.com"
  }'
```

### 3. Set Environment Variables

Set the required Nebius environment variables:

```bash
export NEBIUS_TENANT_ID='tenant-...'
export NEBIUS_PROJECT_ID='project-...'
export NEBIUS_REGION='eu-north1'  # or 'us-central1'
```

Then source `environment.sh` to auto-discover infrastructure and configure the Terraform backend:

```bash
source environment.sh
```

This will:
- Get an IAM token via the Nebius CLI
- Auto-discover your VPC subnet
- Create (or reuse) an S3 bucket for remote Terraform state
- Create a service account with a temporary access key
- Generate `terraform_backend_override.tf` for S3 backend storage
- Export all required `TF_VAR_*` variables

### 4. Configure `terraform.tfvars`

Nebius infrastructure variables (`project_id`, `region`, `subnet_id`, `viewers_group_id`, `iam_token`) are set automatically by `environment.sh` — do not add them to `terraform.tfvars`.

You must also specify `node_pools` — without this, no compute nodes will be created:

```hcl
node_pools = [
  { platform = "cpu-d3", preset = "4vcpu-16gb" },
  { platform = "cpu-d3", preset = "16vcpu-64gb" },
  { platform = "gpu-h200-sxm", preset = "1gpu-16vcpu-200gb" },
  { platform = "gpu-h200-sxm", preset = "8gpu-128vcpu-1600gb", infiniband_fabric = "fabric-7" },
]
```

### 5. Deploy Infrastructure

```bash
terraform init
terraform plan
terraform apply
```

Deployment typically takes 15-30 minutes.
