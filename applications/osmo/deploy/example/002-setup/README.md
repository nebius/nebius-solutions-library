# Kubernetes Setup Scripts

This directory contains scripts for configuring the Kubernetes cluster with GPU infrastructure and OSMO components.

For a single place to configure hostnames, Nebius SSO, and the DB password:

1. **Copy and edit the deploy env file:**
   ```bash
   cp osmo-deploy.env.example osmo-deploy.env
   # Edit osmo-deploy.env: set OSMO_INGRESS_HOSTNAME (e.g. osmo.<LB_IP_DASHED>.nip.io), NEBIUS_SSO_CLIENT_ID, NEBIUS_SSO_CLIENT_SECRET, OSMO_POSTGRESQL_PASSWORD
   ```
2. **Get your LoadBalancer IP** (for nip.io):  
   `kubectl get svc -n ingress-nginx ingress-nginx-controller -o jsonpath='{.status.loadBalancer.ingress[0].ip}'`  
   Use dashed form in the hostname (e.g. `89.169.122.246` → `89-169-122-246`).
3. **Run in order** (scripts load `osmo-deploy.env` automatically via `defaults.sh`):
   ```bash
   ./04-enable-tls.sh osmo.89-169-122-246.nip.io   # or your OSMO_INGRESS_HOSTNAME from osmo-deploy.env
   ./05-deploy-osmo-control-plane.sh
   ./06-deploy-osmo-backend.sh
   ```
4. Open **https://\<your-hostname\>** (e.g. `https://osmo.89-169-122-246.nip.io`) in the browser—no `/etc/hosts` needed with nip.io.

OIDC client redirect URI must match your Keycloak hostname (e.g. `auth-osmo.89-169-122-246.nip.io`). See [applications/osmo/iam-register/README.md](../../../iam-register/README.md).

### Finding your nip.io address

The hostname is `osmo.<dashed-ip>.nip.io` where `<dashed-ip>` is your ingress LoadBalancer IP with dots replaced by dashes (e.g. `89.169.120.232` → `89-169-120-232`). To print the full OSMO URL in one command:

```bash
# From repo root or 002-setup; uses default namespace ingress-nginx
IP=$(kubectl get svc -n ingress-nginx -l app.kubernetes.io/name=ingress-nginx,app.kubernetes.io/component=controller -o jsonpath='{.items[0].status.loadBalancer.ingress[0].ip}' 2>/dev/null)
[[ -n "$IP" ]] && echo "https://osmo.${IP//./-}.nip.io" || echo "Could not get LoadBalancer IP (is ingress deployed?)"
```

Scripts that source `defaults.sh` (e.g. 04, 05, 07) auto-set `OSMO_INGRESS_HOSTNAME` from this value when unset.

## Prerequisites

1. Complete infrastructure deployment (001-iac)
2. kubectl configured with cluster access:
   ```bash
   nebius mk8s cluster get-credentials --id <cluster-id> --external
   ```

## Deployment Order

Run scripts in order:

```bash
# 1. GPU Infrastructure (GPU Operator, Network Operator, KAI Scheduler)
./01-deploy-gpu-infrastructure.sh

# 2. Observability (Prometheus, Grafana, Loki)
./02-deploy-observability.sh

# 3. NGINX Ingress Controller (required – provides routing for OSMO services)
./03-deploy-nginx-ingress.sh

# 4. Enable TLS (optional, recommended)
./04-enable-tls.sh <hostname>   # omit <hostname> to use OSMO_INGRESS_HOSTNAME

# 5. OSMO Control Plane
./05-deploy-osmo-control-plane.sh

# 6. OSMO Backend
./06-deploy-osmo-backend.sh

# 7. Configure Storage (requires port-forward, see main README)
./07-configure-storage.sh

# 8. Configure GPU Platform (required for GPU workflows)
./09-configure-gpu-platform.sh
```

## Scripts

| Script | Purpose | Duration |
|--------|---------|----------|
| `01-deploy-gpu-infrastructure.sh` | GPU Operator, Network Operator, KAI Scheduler | ~15 min |
| `02-deploy-observability.sh` | Prometheus, Grafana, Loki, Promtail | ~10 min |
| `03-deploy-nginx-ingress.sh` | NGINX Ingress Controller (routing for OSMO services) | ~2 min |
| `05-deploy-osmo-control-plane.sh` | OSMO Control Plane, Ingress resources, database secrets, service URL | ~5 min |
| `06-deploy-osmo-backend.sh` | OSMO Backend operator | ~5 min |
| `07-configure-storage.sh` | Configure S3-compatible storage for workflow logs/data | ~1 min |
| `07-configure-service-url.sh` | Reconfigure service URL manually (usually not needed) | ~1 min |
| `09-configure-gpu-platform.sh` | Configure GPU platform with tolerations/node selector | ~1 min |

## Configuration

### Helm Values

Customize deployments by editing files in `values/`:

| File | Component |
|------|-----------|
| `gpu-operator.yaml` | NVIDIA GPU Operator |
| `network-operator.yaml` | NVIDIA Network Operator |
| `kai-scheduler.yaml` | KAI GPU Scheduler |
| `prometheus.yaml` | Prometheus + Grafana |
| `loki.yaml` | Loki Log Aggregation |
| `promtail.yaml` | Log Collection |

### Environment Variables

Configure via `defaults.sh` or export before running:

```bash
# Namespaces
GPU_OPERATOR_NAMESPACE="gpu-operator"
NETWORK_OPERATOR_NAMESPACE="network-operator"
MONITORING_NAMESPACE="monitoring"
OSMO_NAMESPACE="osmo"

# Grafana password (auto-generated if empty)
GRAFANA_ADMIN_PASSWORD=""

# NGINX Ingress (deploy 03-deploy-nginx-ingress.sh before 05-deploy-osmo-control-plane.sh)
OSMO_INGRESS_HOSTNAME=""         # hostname for Ingress rules (e.g. osmo.example.com); leave empty for IP-based access
OSMO_INGRESS_BASE_URL=""         # override for service_base_url; auto-detected from LoadBalancer if empty

# Keycloak / Nebius SSO (see AUTHENTICATION.md for full details)
DEPLOY_KEYCLOAK="true"           # set false to disable Keycloak (dev/open API)
NEBIUS_SSO_ENABLED="false"       # set true to add Nebius SSO as a login option (local osmo-admin user still created by default)
NEBIUS_SSO_ISSUER_URL=""         # OIDC issuer URL (e.g. https://auth.example.com/realms/corporate)
NEBIUS_SSO_CLIENT_ID=""          # OAuth client ID in Nebius SSO
NEBIUS_SSO_CLIENT_SECRET=""      # OAuth client secret (or create secret keycloak-nebius-sso-secret)
```

### Secrets from MysteryBox

If you ran `secrets-init.sh` in the prerequisites step, the following environment variables are set:

| Variable | Description |
|----------|-------------|
| `TF_VAR_postgresql_mysterybox_secret_id` | MysteryBox secret ID for PostgreSQL password |
| `TF_VAR_mek_mysterybox_secret_id` | MysteryBox secret ID for MEK (Master Encryption Key) |

The `05-deploy-osmo-control-plane.sh` script automatically reads these secrets from MysteryBox. This keeps sensitive credentials out of Terraform state and provides a secure secrets management workflow.

**Secret retrieval order:**
1. **MysteryBox** (if secret ID is set via `TF_VAR_*` or `OSMO_*` env vars)
2. **Terraform outputs** (fallback)
3. **Environment variables** (fallback)
4. **Interactive prompt** (last resort)

To manually retrieve secrets from MysteryBox:
```bash
# PostgreSQL password
nebius mysterybox v1 payload get-by-key \
  --secret-id $TF_VAR_postgresql_mysterybox_secret_id \
  --key password --format json | jq -r '.data.string_value'

# MEK (Master Encryption Key)
nebius mysterybox v1 payload get-by-key \
  --secret-id $TF_VAR_mek_mysterybox_secret_id \
  --key mek --format json | jq -r '.data.string_value'
```

## Authentication (Keycloak and Nebius SSO)

Authentication is handled by **Keycloak**. You can use either:

- **Local users (default):** A test user `osmo-admin` / `osmo-admin` is created when Nebius SSO is not enabled. Suitable for dev/test.
- **Nebius System SSO (recommended for production):** Set `NEBIUS_SSO_ENABLED=true` and provide Nebius SSO OIDC settings. Keycloak shows a "Nebius SSO" button on the login page alongside local login. A local `osmo-admin` user is still created by default (for backend automation); set `CREATE_OSMO_TEST_USER=false` to suppress it.
- **Google / GitHub / Microsoft SSO (optional):** Set `GOOGLE_SSO_CLIENT_ID` + `GOOGLE_SSO_CLIENT_SECRET`, and/or `GITHUB_SSO_CLIENT_ID` + `GITHUB_SSO_CLIENT_SECRET`, and/or `MICROSOFT_SSO_CLIENT_ID` + `MICROSOFT_SSO_CLIENT_SECRET` in `osmo-deploy.env`. Create OAuth apps in [Google Cloud Console](https://console.cloud.google.com/apis/credentials), [GitHub Developer Settings](https://github.com/settings/developers), and [Azure Portal App registrations](https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps/ApplicationsListBlade). Set the redirect URI in each provider to `https://<AUTH_DOMAIN>/realms/osmo/broker/<alias>/endpoint` with alias `google`, `github`, or `microsoft`. Re-run `05-deploy-osmo-control-plane.sh` to register the IdPs.

See **[AUTHENTICATION.md](AUTHENTICATION.md)** for:

- Authentication flow (browser, API, backend)
- Enabling Nebius SSO and required redirect URIs
- Role and group mapping (Admin, User, Backend Operator)
- TLS and compatibility with OSMO control plane and UI

## Accessing Services

### Grafana Dashboard

```bash
kubectl port-forward -n monitoring svc/prometheus-grafana 3000:80
# Open http://localhost:3000
# User: admin
# Password: (shown during deployment or in defaults.sh)
```

### Prometheus

```bash
kubectl port-forward -n monitoring svc/prometheus-kube-prometheus-prometheus 9090:9090
# Open http://localhost:9090
```

### OSMO API

```bash
kubectl port-forward -n osmo svc/osmo-service 8080:80
# Open http://localhost:8080
```

### OSMO Web UI

```bash
kubectl port-forward -n osmo svc/osmo-ui 8081:80
# Open http://localhost:8081
```

## Cleanup

Run cleanup scripts in reverse order:

```bash
cd cleanup

# Remove OSMO
./uninstall-osmo-backend.sh
./uninstall-osmo-control-plane.sh

# Remove observability
./uninstall-observability.sh

# Remove GPU infrastructure
./uninstall-gpu-infrastructure.sh
```

## Configure OSMO GPU Platform

After deploying OSMO backend, configure the GPU platform so OSMO can schedule workloads on GPU nodes.

### Why is this needed?

Nebius GPU nodes have a taint `nvidia.com/gpu=true:NoSchedule` that prevents pods from being scheduled unless they have matching tolerations. OSMO needs to be configured with:

1. A **pod template** with GPU tolerations and node selector
2. A **GPU platform** that references this pod template

### Option 1: Run the Configuration Script (Recommended)

```bash
./09-configure-gpu-platform.sh
```

### Option 2: Manual Configuration via API

With port-forward running (`kubectl port-forward -n osmo svc/osmo-service 8080:80`):

**Step 1: Create GPU Pod Template**

```bash
curl -X PUT 'http://localhost:8080/api/configs/pod_template/gpu_tolerations' \
  -H 'Content-Type: application/json' \
  -d @gpu_pod_template.json
```

Where `gpu_pod_template.json` contains:

```json
{
  "configs": {
    "spec": {
      "tolerations": [
        {
          "key": "nvidia.com/gpu",
          "operator": "Exists",
          "effect": "NoSchedule"
        }
      ],
      "nodeSelector": {
        "nvidia.com/gpu.present": "true"
      }
    }
  }
}
```

**Step 2: Create GPU Platform**

```bash
curl -X PUT 'http://localhost:8080/api/configs/pool/default/platform/gpu' \
  -H 'Content-Type: application/json' \
  -d @gpu_platform_update.json
```

Where `gpu_platform_update.json` contains:

```json
{
  "configs": {
    "description": "GPU platform for L40S nodes",
    "host_network_allowed": false,
    "privileged_allowed": false,
    "allowed_mounts": [],
    "default_mounts": [],
    "default_variables": {
      "USER_GPU": 1
    },
    "resource_validations": [],
    "override_pod_template": ["gpu_tolerations"]
  }
}
```

### Verify Configuration

```bash
# Check pod templates
curl -s http://localhost:8080/api/configs/pod_template | jq 'keys'
# Should include: "gpu_tolerations"

# Check GPU platform
curl -s http://localhost:8080/api/configs/pool/default | jq '.platforms.gpu'

# Check resources (GPU nodes should now be visible)
curl -s http://localhost:8080/api/resources | jq '.resources[] | {name: .name, gpu: .allocatable_fields.gpu}'
```

### Using GPU in Workflows

Specify `platform: gpu` in your OSMO workflow:

```yaml
workflow:
  name: my-gpu-job
  resources:
    gpu-resource:
      platform: gpu    # <-- Selects GPU platform with tolerations
      gpu: 1
      memory: 4Gi
  tasks:
  - name: train
    image: nvcr.io/nvidia/cuda:12.6.3-base-ubuntu24.04
    command: ["nvidia-smi"]
    resource: gpu-resource
```

## Troubleshooting

### CLI "Read timed out" or 504 "upstream request timeout"

`05-deploy-osmo-control-plane.sh` patches the osmo-service ingress with 300s proxy timeouts (read/send) so long API calls (auth, workflow submit, resource list) do not hit upstream timeout on a fresh install. If you see 504 on an existing cluster that was deployed before this change, re-run `./05-deploy-osmo-control-plane.sh` to re-apply the patch, or run the kubectl patch once (see 04 script for the exact annotation payload). If the CLI still times out at 60s, the bottleneck is the client.

### "Value 1 too high for CPU" on workflow submit

The default pool may expose only GPU nodes, where allocatable CPU is small (e.g. 3 = 0.3 core). Requesting `cpu: 1` then fails. Use `cpu: 0` in the task resources to use the pool minimum (see `workflows/hello_osmo_minimal.yaml`). If the API rejects `cpu: 0`, add a CPU-only pool or use a GPU workflow (e.g. `workflows/osmo/gpu_test.yaml`).

### GPU Nodes Not Ready

1. Check GPU operator pods:
   ```bash
   kubectl get pods -n gpu-operator
   ```

2. Check node labels:
   ```bash
   kubectl get nodes -l node-type=gpu --show-labels
   ```

3. Check DCGM exporter:
   ```bash
   kubectl logs -n gpu-operator -l app=nvidia-dcgm-exporter
   ```

### Pods Pending on GPU Nodes

1. Verify tolerations:
   ```bash
   kubectl describe pod <pod-name> | grep -A5 Tolerations
   ```

2. Check node taints:
   ```bash
   kubectl describe node <gpu-node> | grep Taints
   ```

### InfiniBand Issues

1. Check Network Operator:
   ```bash
   kubectl get pods -n network-operator
   ```

2. Verify RDMA devices:
   ```bash
   kubectl exec -n gpu-operator <dcgm-pod> -- ibstat
   ```

### Database Connection Failed

1. Verify PostgreSQL is accessible:
   ```bash
   kubectl get secret osmo-database -n osmo -o yaml
   ```

2. Test connection from a pod:
   ```bash
   kubectl run pg-test --rm -it --image=postgres:16 -- psql -h <host> -U <user> -d <db>
   ```

3. If you see **Operation timed out** (cluster cannot reach the DB host): Terraform places both the Kubernetes cluster and managed PostgreSQL in the same network (`network_id` / `subnet_id` from 000-prerequisites), so connectivity should work. If it doesn't, verify in the Nebius console: (a) PostgreSQL and Kubernetes use the same network, (b) no firewall or "allowed networks" is blocking the cluster subnet, (c) the DB is in "Running" state. If all look correct, consider opening a Nebius support ticket (MSP PostgreSQL private endpoint reachability from same-VPC Kubernetes). To continue deploying the rest of 04 (Keycloak, Envoy, etc.) while resolving this, run:
   ```bash
   SKIP_POSTGRES_CONNECTION_TEST=1 ./05-deploy-osmo-control-plane.sh
   ```
   After fixing network, re-run 04 without `SKIP_POSTGRES_CONNECTION_TEST` to verify the connection.

### OSMO Not Seeing GPU Resources

If OSMO shows 0 GPUs or GPU workflows fail to schedule:

1. Check if GPU platform is configured:
   ```bash
   curl -s http://localhost:8080/api/configs/pool/default | jq '.platforms | keys'
   # Should include "gpu"
   ```

2. Check if GPU pod template exists:
   ```bash
   curl -s http://localhost:8080/api/configs/pod_template | jq 'keys'
   # Should include "gpu_tolerations"
   ```

3. Check GPU node labels and taints:
   ```bash
   kubectl describe node <gpu-node> | grep -E 'Taints:|nvidia.com/gpu'
   # Should show taint: nvidia.com/gpu=true:NoSchedule
   # Should show label: nvidia.com/gpu.present=true
   ```

4. If missing, run the GPU configuration:
   ```bash
   ./09-configure-gpu-platform.sh
   ```

5. Verify OSMO sees GPU resources:
   ```bash
   curl -s http://localhost:8080/api/resources | jq '.resources[] | select(.allocatable_fields.gpu != null)'
   ```
