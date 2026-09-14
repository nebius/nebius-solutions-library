# Prerequisites

This directory contains scripts to set up your environment for deploying OSMO on Nebius.

## Scripts

| Script | Purpose |
|--------|---------|
| `install-tools.sh` | Install required tools (Terraform, kubectl, Helm, Nebius CLI, OSMO CLI) |
| `nebius-env-init.sh` | Configure Nebius environment variables |
| `secrets-init.sh` | **NEW** Generate and store secrets in MysteryBox |
| `wireguard-client-setup.sh` | Set up WireGuard VPN client |

## Quick Start

### 1. Install Required Tools

```bash
# Install all required tools
./install-tools.sh

# Or check what's already installed
./install-tools.sh --check
```

### 2. Configure Nebius Environment

```bash
# Source the script (don't just run it)
source ./nebius-env-init.sh
```

This will:
1. Check Nebius CLI installation
2. Verify authentication status
3. Prompt for tenant ID
4. Let you choose to use an existing project OR create a new one
5. Set environment variables for Terraform

#### Project Options

When prompted for a project, you can:
- **Option 1**: Enter an existing project ID directly
- **Option 2**: Create a new project (enter a name)
- **Option 3**: List existing projects first, then choose

Example creating a new project:
```
Project Configuration

Options:
  1) Use existing project (enter project ID)
  2) Create new project
  3) List existing projects first

Choose option [1/2/3]: 2
Enter new project name: osmo-dev
Creating new project: osmo-dev
[✓] Project created successfully
  Project ID: project-abc123xyz
```

### 3. Initialize Secrets (Recommended)

```bash
# Generate secrets and store in MysteryBox
source ./secrets-init.sh
```

This creates:
- **PostgreSQL password** - Stored in MysteryBox, NOT in Terraform state
- **MEK (Master Encryption Key)** - For OSMO service authentication

> **Why?** Storing secrets in MysteryBox keeps them out of Terraform state, providing better security and enabling rotation without re-deploying.

## Nebius CLI Authentication

### First-Time Setup

If you haven't authenticated the Nebius CLI yet:

```bash
# Create a profile (interactive)
nebius profile create
```

The CLI will:
1. Ask for a profile name
2. Open a browser for authentication
3. Ask you to select tenant and project

### WSL Users

If the browser doesn't open automatically in WSL:
1. Copy the URL displayed in the terminal
2. Paste it into your Windows browser
3. Complete the authentication
4. Return to the terminal

### Service Account Authentication

For CI/CD or automated deployments, use service account authentication:

1. **Create a service account** in Nebius Console
2. **Create an authorized key** (PEM file)
3. **Configure the CLI**:
   ```bash
   nebius profile create --auth-type service-account \
     --service-account-id <sa-id> \
     --key-file <path-to-key.pem>
   ```

See [Nebius Service Accounts Documentation](https://docs.nebius.com/iam/service-accounts) for details.

## Required Permissions

Your Nebius account needs these permissions:

### Compute
- `compute.instances.create/delete` - VMs for WireGuard, bastion
- `compute.disks.create/delete` - Boot and data disks
- `compute.filesystems.create/delete` - Shared filesystems

### Kubernetes
- `mk8s.clusters.create/delete` - Kubernetes clusters
- `mk8s.nodeGroups.create/delete` - Node groups

### Networking
- `vpc.networks.create/delete` - VPC networks
- `vpc.subnets.create/delete` - Subnets
- `vpc.publicIpAllocations.create/delete` - Public IPs (for WireGuard)

### Storage
- `storage.buckets.create/delete` - Object storage

### Database
- `mdb.clusters.create/delete` - Managed PostgreSQL

### IAM
- `iam.serviceAccounts.create/delete` - Service accounts
- `iam.accessKeys.create/delete` - Access keys for S3

### Container Registry
- `container-registry.registries.create/delete` - Container registries

See [Nebius IAM Roles](https://docs.nebius.com/iam/authorization/roles) for predefined roles.

## Secrets Management

### Using MysteryBox (Recommended)

The `secrets-init.sh` script generates secrets and stores them in Nebius MysteryBox:

```bash
source ./secrets-init.sh
```

This will:
1. Check if secrets already exist in MysteryBox (by name)
2. If not, generate a secure PostgreSQL password (32 chars) and MEK
3. Store new secrets in MysteryBox (Nebius secrets manager)
4. Export `TF_VAR_*` environment variables for Terraform

### New Terminal Session

If you start a new terminal session, simply run the script again:

```bash
source ./secrets-init.sh
```

The script will detect existing secrets by name and export their IDs without regenerating them.

### Retrieving Secrets

To retrieve secrets from MysteryBox:

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

### Security Considerations

When using MysteryBox secrets:
- Secrets are **NOT** stored in Terraform state
- Only secret IDs are stored in Terraform
- Secrets are fetched at runtime using ephemeral resources
- The password output will be `null` (retrieve via CLI instead)

### Without MysteryBox

If you don't run `secrets-init.sh`, Terraform will:
1. Generate a random password for PostgreSQL
2. Store the password in Terraform state (less secure)
3. Output the password via `terraform output -json`

## Environment Variables

After running `nebius-env-init.sh`, these variables are set:

| Variable | Description |
|----------|-------------|
| `NEBIUS_TENANT_ID` | Your Nebius tenant ID |
| `NEBIUS_PROJECT_ID` | Your Nebius project ID |
| `NEBIUS_REGION` | Deployment region (default: eu-west2) |
| `TF_VAR_tenant_id` | Terraform variable for tenant |
| `TF_VAR_parent_id` | Terraform variable for project |
| `TF_VAR_region` | Terraform variable for region |

After running `secrets-init.sh`, these additional variables are set:

| Variable | Description |
|----------|-------------|
| `OSMO_POSTGRESQL_SECRET_ID` | MysteryBox secret ID for PostgreSQL password |
| `OSMO_MEK_SECRET_ID` | MysteryBox secret ID for MEK |
| `TF_VAR_postgresql_mysterybox_secret_id` | Terraform variable for PostgreSQL secret |
| `TF_VAR_mek_mysterybox_secret_id` | Terraform variable for MEK secret |

## WireGuard VPN Setup

If you enabled WireGuard VPN in your deployment:

```bash
./wireguard-client-setup.sh
```

This will:
1. Check if WireGuard is installed locally
2. Get server information from Terraform outputs
3. Generate client configuration template
4. Provide instructions for completing setup

### Windows/WSL

For WSL users, install WireGuard on Windows:
1. Download from https://www.wireguard.com/install/
2. Import the generated configuration file
3. Connect through the Windows WireGuard app

## Troubleshooting

### "Nebius CLI not installed"

Run the installer:
```bash
./install-tools.sh
```

Or install manually:
```bash
curl -sSL https://storage.eu-north1.nebius.cloud/nebius/install.sh | bash
export PATH="$HOME/.nebius/bin:$PATH"
```

### "Nebius CLI not authenticated"

Authenticate with:
```bash
nebius profile create
```

### "Permission denied"

Ensure scripts are executable:
```bash
chmod +x *.sh
```

### "Token error" or corrupted token

Clear the token and re-authenticate:
```bash
unset NEBIUS_IAM_TOKEN
nebius profile create
```

### WSL browser doesn't open

1. Copy the URL from the terminal output
2. Paste into your Windows browser manually
3. Complete authentication and return to terminal
