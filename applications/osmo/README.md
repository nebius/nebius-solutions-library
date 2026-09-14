# Physical AI Workflow Orchestration on Nebius Cloud

Deploy [NVIDIA OSMO](https://nvidia.github.io/OSMO/main/user_guide/index.html) on [Nebius AI Cloud](https://nebius.com/ai-cloud) in minutes. Run simulation, training, and edge workflows on the wide variety of Nebius GPU instances—write once in YAML, run anywhere.

## Supported Regions

| Region | Available GPU Platforms |
|--------|----------------------|
| `eu-north1` | gpu-h100-sxm, gpu-h200-sxm, gpu-l40s-a, gpu-l40s-d |
| `eu-north2` | gpu-h200-sxm |
| `eu-west1` | gpu-h200-sxm |
| `eu-west2` | gpu-b300-sxm (NVIDIA B300) |
| `me-west1` | gpu-b200-sxm-a (NVIDIA B200) |
| `uk-south1` | gpu-b300-sxm (NVIDIA B300) |
| `us-central1` | gpu-h200-sxm, gpu-b200-sxm (NVIDIA B200) |

## Known Gaps and TODOs

| Gap | Current Workaround | Status |
|-----|-------------------|--------|
| No managed Redis service | Deploy Redis in-cluster via Helm | Workaround in place |
| MysteryBox lacks K8s CSI integration | Scripts retrieve secrets and create K8s secrets manually | Workaround in place |
| No External DNS service | Manual DNS configuration required | Not addressed |
| No managed SSL/TLS service | Manual certificate management | Not addressed |
| No public Load Balancer (ALB/NLB) | Use port-forwarding or WireGuard VPN for access | Workaround in place |
| IDP integration for Nebius | Keycloak + Nebius SSO (OIDC) supported; see [Authentication](deploy/example/002-setup/AUTHENTICATION.md) | Done |
| Nebius Observability Stack integration | Using self-deployed Prometheus/Grafana/Loki | TODO |
| Single cluster for Control Plane + Backend | Using 1 MK8s cluster for both; production separation TBD | Discuss with Nebius |

## What You Get

Production-ready infrastructure-as-code (Terraform) and setup scripts for:
- **Managed Kubernetes (MK8s)** cluster with GPU and CPU node groups
- **GPU Infrastructure** including GPU Operator, Network Operator, and KAI Scheduler
- **Observability Stack** with Prometheus, Grafana, and Loki
- **OSMO Control Plane and Backend** for workflow orchestration
- **Supporting Services** including PostgreSQL, Object Storage, Filestore, and Container Registry
- **Secure Access** via WireGuard VPN (optional)

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              Nebius AI Cloud                                     │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  ┌─────────────────────────────────────────────────────────────────────────┐    │
│  │                         Nebius VPC Network                               │    │
│  │                                                                          │    │
│  │  ┌─────────────┐  ┌────────────────────────────────────────────────┐    │    │
│  │  │  WireGuard  │  │        Nebius Managed Kubernetes (MK8s)        │    │    │
│  │  │    VPN      │  │                                                │    │    │
│  │  │ (Optional)  │  │  ┌────────────────────────────────────────┐   │    │    │
│  │  └──────┬──────┘  │  │           OSMO Namespace                │   │    │    │
│  │         │         │  │  ┌──────────┐ ┌────────┐ ┌──────────┐  │   │    │    │
│  │         │         │  │  │  osmo-   │ │ osmo-  │ │  osmo-   │  │   │    │    │
│  │         │         │  │  │ service  │ │ logger │ │  agent   │  │   │    │    │
│  │         │         │  │  └────┬─────┘ └───┬────┘ └────┬─────┘  │   │    │    │
│  │         │         │  │       └────────────┼──────────┘        │   │    │    │
│  │         │         │  │              ┌─────┴─────┐              │   │    │    │
│  │         │         │  │              │osmo-proxy │              │   │    │    │
│  │         │         │  │              │  (nginx)  │              │   │    │    │
│  │         │         │  │              └─────┬─────┘              │   │    │    │
│  │         │         │  │                    │                    │   │    │    │
│  │         │         │  │  ┌─────────┐  ┌────┴────┐  ┌─────────┐ │   │    │    │
│  │         │         │  │  │ osmo-ui │  │osmo-ctrl│  │osmo-    │ │   │    │    │
│  │         │         │  │  │ (Web UI)│  │(sidecar)│  │backend  │ │   │    │    │
│  │         │         │  │  └─────────┘  └─────────┘  └─────────┘ │   │    │    │
│  │         │         │  └────────────────────────────────────────┘   │    │    │
│  │         │         │                                                │    │    │
│  │         └─────────┼───► ┌──────────────┐  ┌───────────────────┐   │    │    │
│  │                   │     │  CPU Nodes   │  │    GPU Nodes      │   │    │    │
│  │                   │     │  (cpu-d3)    │  │ (L40S/H100/H200/ │   │    │    │
│  │                   │     │             │  │  B200/B300)       │   │    │    │
│  │                   │     │  System pods │  │  Workflow pods    │   │    │    │
│  │                   │     └──────────────┘  └───────────────────┘   │    │    │
│  │                   │                                                │    │    │
│  │                   │  ┌────────────────────────────────────────┐   │    │    │
│  │                   │  │         Infrastructure Stack           │   │    │    │
│  │                   │  │  GPU Operator, Network Operator, Cilium│   │    │    │
│  │                   │  │  Prometheus, Grafana, Loki             │   │    │    │
│  │                   │  └────────────────────────────────────────┘   │    │    │
│  │                   └────────────────────────────────────────────────┘    │    │
│  └─────────────────────────────────────────────────────────────────────────┘    │
│                                                                                  │
│  ┌───────────────┐ ┌───────────────┐ ┌───────────────┐ ┌───────────────┐        │
│  │   Managed     │ │    Object     │ │    Shared     │ │   Container   │        │
│  │  PostgreSQL   │ │   Storage     │ │  Filesystems  │ │   Registry    │        │
│  │  (OSMO DB)    │ │ (Workflow     │ │  (Datasets)   │ │   (Images)    │        │
│  │               │ │  logs/data)   │ │               │ │               │        │
│  └───────────────┘ └───────────────┘ └───────────────┘ └───────────────┘        │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

**Nebius Services Used:**

| Service | Purpose |
|---------|---------|
| MK8s | Managed Kubernetes with CPU and GPU node groups |
| Managed PostgreSQL | Database for OSMO state and metadata |
| Object Storage | S3-compatible storage for workflow logs and artifacts |
| Shared Filesystems | NFS storage for datasets across nodes |
| Compute | VMs for WireGuard VPN (optional) |
| VPC | Private networking with subnet isolation |
| IAM | Service accounts and access keys |
| MysteryBox | Secrets management for credentials |
| Container Registry | Docker image storage for custom workflow images |

## Prerequisites

Before deploying, ensure you have access to Nebius AI Cloud and the required command-line tools installed. The deployment uses Terraform for infrastructure provisioning and Helm/kubectl for Kubernetes configuration.

- [**Nebius Account**](https://console.eu.nebius.com/) with appropriate permissions (see [Required Permissions](#required-permissions))
- [**Nebius CLI**](https://docs.nebius.com/cli/install) installed and authenticated
- [**Terraform**](https://developer.hashicorp.com/terraform/install) >= 1.5.0 for infrastructure-as-code
- [**kubectl**](https://kubernetes.io/docs/tasks/tools/) >= 1.28 for Kubernetes cluster management (should match cluster version ±1 minor)
- [**Helm**](https://helm.sh/docs/intro/install/) >= 3.0 for deploying OSMO charts
- **SSH key pair** for node access (generate with `ssh-keygen` if needed)

## Quick Start

> **Important:** Complete all steps in the **same terminal session**. The setup scripts export environment variables that must persist across steps.

Please run this from a Linux Shell/Ubuntu/WSL.

### 1. Install Required Tools

```bash
cd deploy/000-prerequisites
./install-tools.sh        # Installs: Terraform, kubectl, Helm, Nebius CLI, OSMO CLI
./install-tools.sh --check  # Verify without installing
```

Supports Linux, WSL, and macOS. Requires Python/pip for OSMO CLI installation. See [prerequisites README](deploy/000-prerequisites/README.md) for manual installation.

### 2. Configure Nebius Environment

> **Note:** If not authenticated, run `nebius profile create` first and follow the authentication flow.

```bash
source ./nebius-env-init.sh
```

This interactive script:
1. **Checks Nebius CLI** - Verifies installation and adds to PATH if needed
2. **Checks authentication** - If not authenticated, provides instructions to run `nebius profile create`
3. **Lists tenants** - Auto-detects if you have only one tenant
4. **Configures project** - Select existing project, create new one, or list available projects
5. **Sets region** - Choose from `eu-north1`, `eu-north2`, `eu-west1`, `eu-west2`, `me-west1`, `uk-south1`, `us-central1`
6. **Exports environment variables** - Sets `NEBIUS_*` and `TF_VAR_*` variables for Terraform

### 3. Initialize Secrets (REQUIRED)

```bash
source ./secrets-init.sh
```

> **Important:** This step is **REQUIRED** before running Terraform. If you skip it, `terraform apply` will fail with a clear error message.

This generates secure credentials and stores them in [Nebius MysteryBox](https://docs.nebius.com/mysterybox):
- **PostgreSQL password** - Used by Managed PostgreSQL and OSMO
- **MEK (Master Encryption Key)** - Used by OSMO for data encryption

The script exports `TF_VAR_*` environment variables that Terraform and setup scripts use to retrieve these secrets securely, keeping them out of Terraform state.

### 4. Deploy Infrastructure

Provision all Nebius cloud resources using Terraform: VPC network, Managed Kubernetes cluster, GPU/CPU node groups, PostgreSQL database, Object Storage, and optionally WireGuard VPN.

```bash
cd ../001-iac

# Recommended: Cost-optimized for development (see Appendix A)
cp terraform.tfvars.cost-optimized.example terraform.tfvars

# Edit terraform.tfvars if needed
terraform init
terraform plan -out plan.out
terraform apply plan.out
```

> **Note:** If you get an error about missing `postgresql_mysterybox_secret_id`, go back to step 3 and run `source ./secrets-init.sh`.

See [Terraform README](deploy/001-iac/README.md) for configuration options, and [Appendix A](#appendix-a-terraform-configuration-presets) for preset comparisons.

### 5. Configure Kubernetes

1. Get Kubernetes credentials:
   ```bash
   nebius mk8s cluster get-credentials --id <cluster-id> --external
   ```

2. Verify cluster access:
   ```bash
   kubectl get nodes
   ```

3. Deploy GPU infrastructure and observability:
   ```bash
   cd ../002-setup
   ./01-deploy-gpu-infrastructure.sh
   ./02-deploy-observability.sh
   ```
   
   This installs:
   - NVIDIA GPU Operator and Network Operator
   - KAI Scheduler for GPU workload scheduling
   - Prometheus, Grafana, and Loki for monitoring

4. Deploy NGINX Ingress Controller:
   ```bash
   ./03-deploy-nginx-ingress.sh
   ```
   
   This deploys the community NGINX Ingress Controller with a LoadBalancer IP. It provides path-based routing to all OSMO services (API, router, Web UI). The LoadBalancer IP is auto-detected by later scripts.

5. Deploy OSMO control plane:
   ```bash
   ./04-deploy-osmo-control-plane.sh
   ```
   
   This deploys the core OSMO services:
   - Creates `osmo` namespace and PostgreSQL/MEK secrets
   - Initializes databases on Nebius Managed PostgreSQL
   - Deploys Redis and OSMO services (API, agent, worker, logger)
   - Creates Kubernetes Ingress resources for path-based routing via the NGINX Ingress Controller
   
   > **Note:** The script automatically retrieves PostgreSQL password and MEK from MysteryBox if you ran `secrets-init.sh` earlier.

7. Deploy OSMO backend operator:
   ```bash
   ./05-deploy-osmo-backend.sh
   ```
   
   The script automatically:
   - Starts a port-forward to OSMO service
   - Logs in via Keycloak (or dev method if Keycloak is disabled)
   - Creates a service token for the backend operator
   - Deploys the backend operator
   - Cleans up the port-forward
   
   This deploys the backend operator that manages GPU workloads:
   - Connects to OSMO control plane via `osmo-agent`
   - Configures resource pools for GPU nodes
   - Enables workflow execution on the Kubernetes cluster
   
   > **Manual alternative:** If you prefer to create the token manually, set `OSMO_SERVICE_TOKEN` environment variable before running the script.

8. Verify backend deployment:
   
   Verify the backend is registered with OSMO using the NGINX Ingress LoadBalancer IP:
   ```bash
   # Check backend registration
   curl http://<INGRESS_LB_IP>/api/configs/backend
   
   # Or via OSMO CLI
   osmo config show BACKEND default
   ```
   
   The Ingress LoadBalancer IP is shown in the output of `04-deploy-osmo-control-plane.sh`.
   You should see the backend configuration with status `ONLINE`.

9. Configure OSMO storage:
   ```bash
   ./06-configure-storage.sh
   ```
   
   The script automatically:
   - Retrieves storage bucket details from Terraform
   - Starts port-forward and logs in to OSMO
   - Configures OSMO to use Nebius Object Storage for workflow artifacts
   - Verifies the configuration
   
   > **Note:** The `osmo-storage` secret (with S3 credentials) was created automatically by `04-deploy-osmo-control-plane.sh`.

10. Access OSMO (via NGINX Ingress LoadBalancer):
   
   The NGINX Ingress Controller exposes OSMO via a LoadBalancer IP. The IP is shown in the output of `04-deploy-osmo-control-plane.sh`, or retrieve it with:
   ```bash
   kubectl get svc -n ingress-nginx ingress-nginx-controller -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
   ```
   
   Access points (replace `<LB_IP>` with your LoadBalancer IP):
   - **OSMO API**: `http://<LB_IP>/api/version`
   - **OSMO Web UI**: `http://<LB_IP>`
   
   Login to OSMO CLI:
   ```bash
   osmo login http://<LB_IP> --method dev --username admin
   ```
   
   > **Fallback:** If the LoadBalancer IP is not reachable, you can use port-forwarding:
   > ```bash
   > kubectl port-forward -n osmo svc/osmo-service 8080:80
   > osmo login http://localhost:8080 --method dev --username admin
   > ```

   > **Note:** The `service_base_url` (required for workflow execution) is automatically configured
   > by `04-deploy-osmo-control-plane.sh` using the NGINX Ingress LoadBalancer IP. If you need to
   > reconfigure it manually, run `./07-configure-service-url.sh`.

11. Configure pool for GPU workloads:
   
   The default pool needs GPU platform configuration to run GPU workflows. This creates a pod template with the correct node selector and tolerations for GPU nodes:
   
   ```bash
   ./08-configure-gpu-platform.sh
   ```
   
   The script:
   - Creates a `gpu_tolerations` pod template with `nvidia.com/gpu.present: true` node selector
   - Updates the GPU platform to reference this pod template
   - Verifies GPU nodes are visible in OSMO
   
   You can verify the configuration:
   ```bash
   osmo config show POOL default
   osmo config show POD_TEMPLATE gpu_tolerations
   ```

12. Run a test workflow (optional):
   
   Verify the complete setup by running a test workflow from the `workflows/osmo/` directory:
   
   ```bash
   # Set the default pool (required before submitting workflows)
   osmo profile set pool default
   
   # Submit the hello world workflow (requires GPU)
   osmo workflow submit ../../workflows/osmo/hello_nebius.yaml
   
   # Or specify the pool explicitly
   osmo workflow submit ../../workflows/osmo/hello_nebius.yaml --pool default
   
   # Check workflow status
   osmo workflow list
   osmo workflow query <workflow-id>
   
   # View workflow logs
   osmo workflow query <workflow-id> --logs
   ```
   
   Available test workflows in `workflows/osmo/`:
   - `hello_nebius.yaml` - Simple GPU hello world
   - `gpu_test.yaml` - GPU validation test



## Configuration Tiers

| Tier | GPU Type | GPU Nodes | Security | Est. Cost/6h |
|------|----------|-----------|----------|--------------|
| **Cost-Optimized Secure** (recommended) | 1x B300 | 1 | WireGuard VPN | Smallest eu-west2 footprint |
| **Cost-Optimized** | 1x B300 | 1 | Public endpoints | Smallest eu-west2 footprint |
| **Standard** | 1x B300 | 1 | Public endpoints | Region-specific |
| **Production** | 8x B300 | 4+ | WireGuard VPN | Region-specific |

**Recommended:** For eu-west2 development, use `terraform.tfvars.cost-optimized-secure.example` for the smallest B300 footprint.

See `deploy/001-iac/terraform.tfvars.*.example` files for all configuration options.

## GPU Options

| Platform | Preset | GPUs | vCPUs | RAM | InfiniBand | Regions |
|----------|--------|------|-------|-----|------------|---------|
| `gpu-l40s-a` | `1gpu-8vcpu-32gb` | 1 | 8 | 32GB | No | eu-north1 |
| `gpu-l40s-d` | `1gpu-8vcpu-32gb` | 1 | 8 | 32GB | No | eu-north1 |
| `gpu-h100-sxm` | `1gpu-16vcpu-200gb` | 1 | 16 | 200GB | No | eu-north1 |
| `gpu-h100-sxm` | `8gpu-128vcpu-1600gb` | 8 | 128 | 1600GB | Yes | eu-north1 |
| `gpu-h200-sxm` | `1gpu-16vcpu-200gb` | 1 | 16 | 200GB | No | eu-north1, eu-north2, eu-west1, us-central1 |
| `gpu-h200-sxm` | `8gpu-128vcpu-1600gb` | 8 | 128 | 1600GB | Yes | eu-north1, eu-north2, eu-west1, us-central1 |
| `gpu-b200-sxm` | `1gpu-20vcpu-224gb` | 1 | 20 | 224GB | No | us-central1 |
| `gpu-b200-sxm` | `8gpu-160vcpu-1792gb` | 8 | 160 | 1792GB | Yes | us-central1 |
| `gpu-b200-sxm-a` | `1gpu-20vcpu-224gb` | 1 | 20 | 224GB | No | me-west1 |
| `gpu-b200-sxm-a` | `8gpu-160vcpu-1792gb` | 8 | 160 | 1792GB | Yes | me-west1 |
| `gpu-b300-sxm` | `1gpu-24vcpu-346gb` | 1 | 24 | 346GB | No | eu-west2, uk-south1 |
| `gpu-b300-sxm` | `8gpu-192vcpu-2768gb` | 8 | 192 | 2768GB | Yes | eu-west2, uk-south1 |

**Recommendation:** In eu-west2, use the single-GPU B300 preset for the smallest development configuration.

## Required Permissions

This deployment uses the [Nebius Terraform Provider](https://docs.nebius.com/terraform-provider/quickstart) to provision cloud resources. Your Nebius account needs the following IAM roles to create and manage the required infrastructure.

### Minimum Required Roles
| Role | Purpose |
|------|---------|
| `compute.admin` | VMs, disks, shared filesystems, GPU clusters |
| `vpc.admin` | VPC networks and subnets |
| `mk8s.admin` | Managed Kubernetes clusters and node groups |
| `storage.admin` | Object Storage buckets |
| `mdb.admin` | Managed PostgreSQL clusters |
| `iam.serviceAccounts.admin` | Service accounts and access keys |
| `container-registry.admin` | Container registries |

### For WireGuard VPN (Optional)
| Role | Purpose |
|------|---------|
| `vpc.publicIpAllocations.admin` | Allocate public IPs for VPN endpoint |

For more information, see [Nebius IAM Roles](https://docs.nebius.com/iam/authorization/roles) and the [Terraform Provider Quickstart](https://docs.nebius.com/terraform-provider/quickstart).

## Security Options

### Option A: WireGuard VPN (Recommended for Production)

Enable private-only access with WireGuard VPN:

```hcl
# In terraform.tfvars
enable_wireguard        = true
enable_public_endpoint  = false
```

After deployment:
```bash
cd deploy/000-prerequisites
./wireguard-client-setup.sh
```

### Option B: Public Endpoints

For development/testing with public access:

```hcl
# In terraform.tfvars
enable_wireguard        = false
enable_public_endpoint  = true
```

## Cost Optimization Tips

1. **Use preemptible GPU nodes** for non-critical workloads (up to 70% savings)
2. **Start with single-GPU nodes** for development
3. **Disable unused components** (Loki, multi-GPU support)
4. **Scale down when not in use**

## Documentation

- [Terraform Infrastructure](deploy/001-iac/README.md)
- [Setup Scripts](deploy/002-setup/README.md)
- [Troubleshooting Guide](docs/troubleshooting.md)
- [Security Guide](docs/SECURITY.md)

## License

Apache License 2.0 - See [LICENSE](LICENSE) for details.

---

## Appendix A: Terraform Configuration Presets

The `deploy/001-iac/` directory includes several pre-configured `terraform.tfvars` examples for different use cases:

| Preset | GPU | WireGuard | Public API | Use Case |
|--------|-----|-----------|------------|----------|
| `terraform.tfvars.cost-optimized.example` | B300 | No | Yes | Development with the smallest eu-west2 GPU preset |
| `terraform.tfvars.cost-optimized-secure.example` | B300 | Yes | No | Development with the smallest eu-west2 GPU preset and VPN-only access |
| `terraform.tfvars.secure.example` | H100 | Yes | No | Staging with full security |
| `terraform.tfvars.production.example` | H200 | Yes | No | Production with maximum performance |
| `terraform.tfvars.example` | H100 | No | Yes | Basic template with all options documented |

> **Note:** All configurations use **private nodes** (no public IPs on K8s nodes). Access is via WireGuard VPN or public K8s API endpoint.

### Key Differences

| Preset | GPU Nodes | CPU Nodes | etcd Size | Preemptible | Security |
|--------|-----------|-----------|-----------|-------------|----------|
| **cost-optimized-secure** | 1x B300 | 2x small | 1 | Yes | VPN only |
| **cost-optimized** | 1x B300 | 2x small | 1 | Yes | Public endpoints |
| **secure** | 8x H100 | 3x medium | 3 | No | VPN only |
| **production** | 4x 8-GPU H200 | 3x large | 3 | No | VPN only |

**Recommendation:** For eu-west2 development, start with `terraform.tfvars.cost-optimized-secure.example`, then scale up as needed.

## Cleanup

To tear down the deployment, see [deploy/README.md](deploy/README.md#cleanup) for detailed instructions. The process involves:

1. Uninstalling Kubernetes components (in reverse order) via scripts in `deploy/002-setup/cleanup/`
2. Destroying infrastructure with `terraform destroy` in `deploy/001-iac/`
