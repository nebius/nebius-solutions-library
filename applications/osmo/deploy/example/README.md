# Deployment Guide

This directory contains all deployment artifacts for OSMO on Nebius.

## Deployment Phases

### Phase 0: Prerequisites (`000-prerequisites/`)

Install required tools and configure your Nebius environment.

```bash
cd 000-prerequisites

# Install required tools (Terraform, kubectl, Helm, Nebius CLI)
./install-tools.sh

# Check if tools are installed
./install-tools.sh --check

# Configure Nebius environment
source ./nebius-env-init.sh

# (Recommended) Initialize secrets in MysteryBox
source ./secrets-init.sh
```

### Phase 1: Infrastructure (`001-iac/`)

Deploy cloud infrastructure using Terraform.

```bash
cd 001-iac

# Recommended: Cost-optimized with secure private access
cp terraform.tfvars.cost-optimized-secure.example terraform.tfvars

# Other options:
#   terraform.tfvars.cost-optimized.example  - Cheapest (public endpoints)
#   terraform.tfvars.production.example      - Full production setup
#   terraform.tfvars.secure.example          - H100 with WireGuard

# Edit configuration
vim terraform.tfvars

# Deploy
terraform init
terraform plan
terraform apply
```

**Resources Created:**
- VPC Network and Subnet
- Managed Kubernetes Cluster
- CPU and GPU Node Groups
- Managed PostgreSQL
- Object Storage Buckets
- Filestore (Shared Filesystem)
- Container Registry
- Service Accounts
- WireGuard VPN (optional)

### Phase 2: Kubernetes Setup (`002-setup/`)

Configure Kubernetes with GPU infrastructure and OSMO.

```bash
cd 002-setup

# 1. Deploy GPU infrastructure
./01-deploy-gpu-infrastructure.sh

# 2. Deploy observability stack
./02-deploy-observability.sh

# 3. Deploy NGINX Ingress Controller
./03-deploy-nginx-ingress.sh

# 4. Enable TLS (optional, recommended – set up DNS A record first)
./03b-enable-tls.sh <hostname>   # omit <hostname> to use OSMO_INGRESS_HOSTNAME

# 5. Deploy OSMO control plane
./04-deploy-osmo-control-plane.sh

# 6. Deploy OSMO backend
./05-deploy-osmo-backend.sh
```

## Directory Structure

```
deploy/
├── 000-prerequisites/
│   ├── install-tools.sh          # Tool installer
│   ├── nebius-env-init.sh        # Environment setup
│   ├── secrets-init.sh           # MysteryBox secrets setup
│   ├── wireguard-client-setup.sh # WireGuard client config
│   └── README.md
├── 001-iac/
│   ├── modules/
│   │   ├── platform/             # VPC, Storage, DB, Registry
│   │   ├── k8s/                  # Kubernetes cluster
│   │   └── wireguard/            # VPN infrastructure
│   ├── main.tf                   # Root module
│   ├── variables.tf              # Input variables
│   ├── outputs.tf                # Output values
│   ├── versions.tf               # Provider versions
│   ├── terraform.tfvars.*.example
│   └── README.md
└── 002-setup/
    ├── lib/
    │   └── common.sh             # Shared functions
    ├── values/                   # Helm values files
    ├── 01-deploy-gpu-infrastructure.sh
    ├── 02-deploy-observability.sh
    ├── 03-deploy-nginx-ingress.sh
    ├── 03b-enable-tls.sh
    ├── 04-deploy-osmo-control-plane.sh
    ├── 05-deploy-osmo-backend.sh
    ├── cleanup/                  # Uninstall scripts
    └── README.md
```

## Configuration Files

| File | Purpose | Recommended |
|------|---------|-------------|
| `terraform.tfvars.cost-optimized-secure.example` | Cheap + secure (L40S + VPN) | **Recommended** |
| `terraform.tfvars.cost-optimized.example` | Cheapest (L40S, public) | Dev only |
| `terraform.tfvars.production.example` | Full production (H200 + VPN) | Production |
| `terraform.tfvars.secure.example` | H100 + VPN | Staging |

## Environment Variables

After running `nebius-env-init.sh`, these variables are set:

| Variable | Description |
|----------|-------------|
| `NEBIUS_TENANT_ID` | Your Nebius tenant ID |
| `NEBIUS_PROJECT_ID` | Your Nebius project ID |
| `NEBIUS_REGION` | Deployment region |
| `TF_VAR_tenant_id` | Terraform variable for tenant |
| `TF_VAR_parent_id` | Terraform variable for project |
| `TF_VAR_region` | Terraform variable for region |

## Cleanup

To remove all deployed resources:

```bash
# 1. Remove Kubernetes components
cd 002-setup/cleanup
./uninstall-osmo-backend.sh
./uninstall-osmo-control-plane.sh
./uninstall-observability.sh
./uninstall-gpu-infrastructure.sh

# 2. Destroy infrastructure
cd ../../001-iac
terraform destroy
```

## Troubleshooting

### Terraform Errors

1. **Authentication failed**: Run `source ../000-prerequisites/nebius-env-init.sh`
2. **Resource quota exceeded**: Check Nebius console for quota limits
3. **Invalid region**: Verify region supports required GPU types

### Kubernetes Errors

1. **Nodes not ready**: Check GPU operator pod logs
2. **Pods pending**: Verify node group scaling
3. **Network issues**: Check Cilium pod status

See [Troubleshooting Guide](../docs/troubleshooting.md) for more details.
