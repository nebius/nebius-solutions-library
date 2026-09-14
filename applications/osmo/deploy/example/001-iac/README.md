# Infrastructure as Code (Terraform)

This directory contains Terraform configurations for deploying OSMO infrastructure on Nebius.

## Prerequisites

1. Install required tools:
   ```bash
   cd ../000-prerequisites
   ./install-tools.sh
   ```

2. Configure Nebius environment:
   ```bash
   source ../000-prerequisites/nebius-env-init.sh
   ```

3. **(Recommended)** Initialize secrets in MysteryBox:
   ```bash
   source ../000-prerequisites/secrets-init.sh
   ```
   This generates secure passwords/keys and stores them in MysteryBox, keeping them OUT of Terraform state.

## Quick Start

```bash
# Recommended: Cost-optimized with secure private access
cp terraform.tfvars.cost-optimized-secure.example terraform.tfvars

# Edit if needed (tenant_id and parent_id set via environment); optional SSH key
vim terraform.tfvars

# Deploy
terraform init
terraform plan
terraform apply
```

## Configuration Tiers

| File | Use Case | GPU | Security | Est. Cost/6h |
|------|----------|-----|----------|--------------|
| `terraform.tfvars.cost-optimized-secure.example` (recommended) | Dev | 1x B300 | WireGuard | Smallest eu-west2 footprint |
| `terraform.tfvars.cost-optimized.example` | Dev | 1x B300 | Public | Smallest eu-west2 footprint |
| `terraform.tfvars.secure.example` | Staging | 8x H100 | WireGuard | ~$300-400 |
| `terraform.tfvars.production.example` | Production | 32x H200 | WireGuard | ~$1000+ |

## Resources Created

### Network
- VPC Network
- Subnet with configurable CIDR

### Kubernetes
- Managed Kubernetes Cluster (MK8s)
- CPU Node Group (for system workloads)
- GPU Node Group(s) (for training)
- Service Account for node groups

### Storage
- Object Storage Bucket (S3-compatible)
- Shared Filesystem (Filestore)
- Service Account with access keys

### Database
- Managed PostgreSQL Cluster

### Container Registry
- Nebius Container Registry (when `enable_container_registry = true`)

### Optional
- WireGuard VPN Server (when `enable_wireguard = true`)
- GPU Cluster for InfiniBand (when `enable_gpu_cluster = true`)

## Module Structure

```
001-iac/
├── main.tf              # Root module
├── variables.tf         # Input variables
├── outputs.tf           # Output values
├── locals.tf            # Local values
├── versions.tf          # Provider versions
├── terraform.tfvars.*.example
└── modules/
    ├── platform/        # VPC, Storage, DB, Container Registry
    ├── k8s/             # Kubernetes cluster
    └── wireguard/       # VPN server
```

## GPU Options

### Available Platforms (eu-west2)

| Platform | GPU | VRAM | ~Cost/hr | Best For |
|----------|-----|------|----------|----------|
| `gpu-b300-sxm` | B300 | 346GB | Region-specific | Training and large models |

### Presets

| Platform | Preset | GPUs | vCPUs | RAM |
|----------|--------|------|-------|-----|
| B300 | `1gpu-24vcpu-346gb` | 1 | 24 | 346GB |
| B300 | `8gpu-192vcpu-2768gb` | 8 | 192 | 2768GB |

## Security Options

### Public Access (Default)

```hcl
enable_public_endpoint     = true
cpu_nodes_assign_public_ip = true
enable_wireguard           = false
```

### Private Access (WireGuard)

```hcl
enable_public_endpoint     = false
cpu_nodes_assign_public_ip = false
gpu_nodes_assign_public_ip = false
enable_wireguard           = true
```

After deployment, set up VPN client:
```bash
cd ../000-prerequisites
./wireguard-client-setup.sh
```

## Cost Optimization

### Use Preemptible GPUs
```hcl
gpu_nodes_preemptible = true  # Up to 70% savings
```

### Use Single-GPU Nodes for Dev
```hcl
gpu_nodes_preset = "1gpu-16vcpu-200gb"
enable_gpu_cluster = false
```

### Minimize Storage
```hcl
filestore_size_gib       = 256
postgresql_disk_size_gib = 20
```

## Secrets Management (MysteryBox)

This module supports two approaches for secrets:

### Option A: MysteryBox (Recommended)
Secrets are stored in Nebius MysteryBox and read at runtime. **Not stored in Terraform state.**

```bash
# Before terraform apply:
cd ../000-prerequisites
source ./secrets-init.sh  # Creates secrets in MysteryBox
cd ../001-iac
terraform apply           # Uses TF_VAR_* env vars set by script
```

**Benefits:**
- Secrets never in Terraform state file
- Centralized secret management
- Easier rotation without re-deploying
- Better audit trail

**Retrieving Secrets:**
```bash
# PostgreSQL password
nebius mysterybox v1 payload get-by-key \
  --secret-id $OSMO_POSTGRESQL_SECRET_ID \
  --key password \
  --format json | jq -r '.data.string_value'

# MEK
nebius mysterybox v1 payload get-by-key \
  --secret-id $OSMO_MEK_SECRET_ID \
  --key mek \
  --format json | jq -r '.data.string_value'
```

### Option B: Terraform-Generated (Fallback)
If MysteryBox secret IDs are not set, Terraform generates secrets automatically.

```hcl
# Secrets stored in Terraform state (less secure)
postgresql_mysterybox_secret_id = null  # Default
mek_mysterybox_secret_id        = null  # Default
```

**Retrieving Secrets:**
```bash
terraform output -json postgresql_password
```

### MysteryBox Variables

| Variable | Description |
|----------|-------------|
| `postgresql_mysterybox_secret_id` | Secret ID for PostgreSQL password |
| `mek_mysterybox_secret_id` | Secret ID for OSMO MEK |

## Outputs

After `terraform apply`, you'll see:

- `cluster_id` - Kubernetes cluster ID
- `cluster_endpoint` - Kubernetes API endpoint
- `storage_bucket` - Object storage details
- `container_registry` - Container Registry details (endpoint, name)
- `postgresql` - Database connection info
- `wireguard` - VPN details (if enabled)
- `next_steps` - Instructions for next deployment phase

## Cleanup

```bash
terraform destroy
```

**Warning**: This will delete all resources including data in PostgreSQL and Object Storage.

## Troubleshooting

### Authentication Error
```bash
source ../000-prerequisites/nebius-env-init.sh
```

### Resource Quota Exceeded
Check your Nebius quota in the console and request increases if needed.

### Invalid GPU Platform
Verify the platform is available in your region:
- `eu-north1`: H100
- `eu-west1`: H200
- `eu-west2`: B300
