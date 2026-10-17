# Run:ai on Nebius AI

Deploy a self-hosted [Run:ai](https://www.nvidia.com/en-us/data-center/runai/) control plane
on a Nebius managed Kubernetes cluster, with a GPU node pool ready for Run:ai workloads.

## Features

- Managed Kubernetes cluster provisioned with Terraform (Nebius mk8s)
- Dedicated CPU system node group for the Run:ai control plane and a GPU node group for workloads
- NVIDIA GPU Operator (repo `modules/gpu-operator`) with DCGM metrics exporter
  (Prometheus `ServiceMonitor` scraping is available in the module but off by default)
- HAProxy ingress controller (the Run:ai-documented ingress class)
- Run:ai control plane (`runai/control-plane` helm chart) with:
  - `self-signed` TLS mode with automatically derived `sslip.io` DNS (default, zero DNS setup)
  - `byo` TLS mode with your own domain and certificate
  - NVIDIA NGC pull secret for control plane images
- Optional phase 2: register the cluster's GPU pool to the control plane (`runai/runai-cluster` chart)

## Prerequisites

- Nebius CLI (`nebius`) authenticated, with `NEBIUS_TENANT_ID`, `NEBIUS_PROJECT_ID`, `NEBIUS_REGION` set
- Terraform >= 1.11 (the gpu-operator module uses the nebius provider's write-only `sensitive` block, which requires Terraform 1.11+)
- An [NGC API key](https://org.ngc.nvidia.com/setup/api-keys) — used for helm repository
  authentication and for pulling Run:ai images from `nvcr.io`
- Run:ai control plane requires a **default StorageClass** and **LoadBalancer** service
  support — both are provided by Nebius mk8s; verify them after cluster creation:

  ```bash
  kubectl get sc
  kubectl get svc -n haproxy
  ```

### Quotas

The default topology needs the following Nebius quotas in the deployment region
(verified against tenant defaults in the Nebius console; check yours under
**Administration → Limits**):

- **Non-GPU vCPUs**: the CPU system nodes draw from the *non-gpu* vCPU quota —
  3 nodes × 16 vCPUs = **48 vCPUs** with the default preset (node presets are
  region-dependent, so check the actual preset). Note: **us-central1 and
  eu-north1 have a default non-gpu vCPU quota of 0** (200 in eu-west1,
  eu-south1, and us-north1) — either deploy in one of those regions or request
  an increase.
- **Public IPv4**: default quota is **3 per region**. Nodes have **no public
  IPs by default**, so the default footprint is 2 (1 mk8s API endpoint IP +
  1 HAProxy LoadBalancer IP). If you enable `cpu_nodes_public_ips` /
  `gpu_nodes_public_ips`, you need 1 per node **plus** those 2 — the default
  quota is not enough; request an increase first.
- **GPU**: default quota is 32 vGPUs per GPU type in most regions; one 8-GPU
  node fits comfortably.

Request increases in the Nebius console under **Administration → Limits →
Requests** (or ask your Nebius support contact).

## Usage

```bash
cd runai
source environment.sh   # creates state bucket/SA, writes terraform_backend_override.tf, exports TF_VAR_*

export TF_VAR_ngc_api_key="..."        # NGC API key
export TF_VAR_runai_admin_password="..."  # >= 8 chars, upper+lower+digit+special

terraform init
terraform plan
terraform apply
```

The Run:ai UI is served at `https://runai.<domain>` (see the `runai_ui_url` output;
allow up to ~15 minutes for control plane pods to become ready). Log in with
`runai_admin_username` / `runai_admin_password`.

In the default `self-signed` mode, `<domain>` is `<ingress-lb-ip>.sslip.io`, derived
automatically from the HAProxy ingress LoadBalancer IP. Your browser will warn about
the self-signed certificate. With `tls_mode = "byo"` you provide the domain and a
matching certificate; the UI is at `https://runai.<runai_domain>`.

**Self-signed bootstrap quirk:** on the very first `terraform apply`, the helm
release waits for pod readiness but not for the cloud load balancer to assign
an IP, so the apply can stop with "HAProxy LoadBalancer IP is not ready yet"
(a plan-time warning plus preconditions on the TLS cert and the control-plane
release enforce this cleanly — it never blocks `terraform destroy`). Wait for
the ingress Service to get an external IP (`kubectl get svc -n haproxy`) and
re-run `terraform apply`; it is idempotent and completes on the second pass.

## Configuration variables

Copy `terraform.tfvars` and adjust. Highlights:

```hcl
cluster_name           = "runai"
cpu_nodes_fixed_count  = 3  # control plane system nodes
gpu_nodes_fixed_count  = 1
runai_version          = "2.26.50"
tls_mode               = "self-signed" # or "byo"
```

For `byo` mode also set `runai_domain`, `tls_certificate`, `tls_private_key`
(environment variables `TF_VAR_tls_certificate` / `TF_VAR_tls_private_key` recommended).

Security- and network-relevant variables (see `variables.tf` for full
descriptions):

- `cpu_nodes_public_ips` / `gpu_nodes_public_ips` (default `false`) — nodes
  have **no public IPv4 addresses** by default. Reach them via a bastion or the
  internal network, or set these to `true` explicitly (mind the 3-per-region
  public-IPv4 quota, see [Quotas](#quotas)).
- `mk8s_allowed_cidrs` — CIDRs allowed to reach the mk8s API endpoint. The
  default (`null`) leaves the endpoint open to the world (Nebius semantics);
  restricting it to your operator networks is strongly recommended.
- `node_group_sa_editors` (default `false`) — adds the node-group service
  account to the tenant-wide `editors` IAM group. Enable **only** if image
  pulls or node operations fail without it: the node SA's credentials are
  readable from every node, so membership grants any compromised node
  tenant-wide editor rights.
- `k8s_version` — pinned to `"1.36"` by default and validated against the
  Run:ai support matrix (1.34–1.36 for Run:ai 2.26; re-check
  https://docs.nvidia.com/runai/ before changing it).
- `helm_timeout` (default `2400`) — helm install/upgrade wait timeout in
  seconds for all releases (the control plane can take ~15 minutes to become
  ready).

Secrets (`ngc_api_key`, `runai_admin_password`, BYO cert/key) are sensitive variables;
prefer `TF_VAR_*` environment variables over committing them to `terraform.tfvars`.

> **Terraform state contains secrets.** The state stored in the Object Storage bucket
> includes the TLS private key, the admin password, and the NGC API key (via the helm
> release values and kubernetes secret resources). Use the encrypted private bucket
> that `environment.sh` creates, restrict access to it, and never copy state files
> into the repo or other storage.
>
> The state bucket is **versioned**: old state versions — each containing the
> secrets as of that write — are retained indefinitely, including after
> credential rotation and even after `terraform destroy`. If you need them
> gone, purge the noncurrent object versions (console or CLI; see
> [Destroy](#destroy)).

## Registering the GPU cluster (phase 2)

The `runai-cluster` chart requires values that are generated by the Run:ai UI
onboarding wizard — there is no API-only bootstrap:

1. Apply with `runai_cluster_enabled = false` (default) and open the UI.
2. Complete the first-run wizard. Then go to **Cluster setup -> Connect a new
   cluster -> Helm** and note `controlPlane.url`, `controlPlane.clientSecret`,
   `cluster.uid`, and `cluster.url`.
3. Set the variables and re-apply:

   ```hcl
   runai_cluster_enabled       = true
   runai_control_plane_url     = "https://runai.<domain>"
   runai_cluster_uid           = "..."
   runai_cluster_url           = "..."
   runai_cluster_client_secret = "..."
   ```

**Control plane with a private/self-signed CA:** if your `byo` certificate
chains to an internal or otherwise non-publicly-trusted CA, the cluster-side
hooks cannot verify the control plane URL. Create the `runai-ca-cert` secret
in namespace `runai` and set `global.customCA.enabled: true` on the
`runai-cluster` chart per the Run:ai documentation before enabling
`runai_cluster_enabled`.

## Connecting to the cluster

```bash
export KUBECONFIG=~/.kube/runai-config
nebius mk8s cluster get-credentials \
  --id "$(terraform output -json kube_cluster | jq -r '.id')" \
  --path "$KUBECONFIG"
```

## Good to know

- The `self-signed` mode depends on `sslip.io`, a free third-party DNS service. If the
  UI doesn't resolve, check whether sslip.io is reachable from your network. Also note
  the domain is baked into the TLS certificate and the control plane configuration —
  if the ingress LoadBalancer IP ever changes (cluster recreation, LB replacement),
  the certificate and `global.domain` change with it, which effectively requires a
  control-plane reinstall. Use `byo` mode for anything that must survive that.
- The pinned chart default (`runai_version = "2.26.50"`) uses `controlPlane.clientSecret`
  for cluster registration. Run:ai 3.x renamed that value to `bootstrapToken` — if you
  bump `runai_version` to 3.x, update `runai/cluster.tf` accordingly.
- Do not edit the `runaiconfig` CR (`kubectl edit runaiconfig runai -n runai`) directly;
  manage cluster configuration through the helm values in `cluster.tf` only.
- Any node-**template** change (disk size, SSH key, preset, `k8s_version`) rolls
  **every node** in that node group — `terraform plan` shows it as an in-place
  update, but apply drains and replaces the nodes (delete-first; there is no
  `strategy` block). Expect workload disruption and plan for it.
- Changing `mig_strategy` reconfigures MIG on live GPU nodes and can reboot
  them — drain workloads first.
- The HAProxy ingress is deployed hardened: the stats (1024), admin (6060) and
  QUIC ports, `/debug/pprof`, and `/metrics` are disabled on the public
  Service; the minimum TLS version is pinned to 1.2 (`no-sslv3 no-tlsv10
  no-tlsv11`); and the controller runs unprivileged with a restricted
  securityContext. Source-IP restriction via the chart's
  `controller.service.loadBalancerSourceRanges` is available if the Nebius
  cloud controller honors it (untested with this CCM).
- If the initial admin password was rejected during install, delete the secret
  `runai-backend-gen-tenants-manager` in namespace `runai-backend` and re-run
  `terraform apply` with a policy-compliant password. The password is validated at
  plan time in this solution to prevent this situation.
- Air-gapped installation is not supported by this solution (requires the NGC
  air-gap package and an internal registry).
- `source environment.sh` creates a new short-lived (1-day) access key on every
  run; delete stale `runai-tfstate-*` keys periodically (see
  [Destroy](#destroy)). Note that secrets you `export` interactively
  (`TF_VAR_ngc_api_key`, ...) land in your shell history — prefer a sourced,
  git-ignored file or a secret manager.

### Troubleshooting

- **Apply fails fast with "another operation (install/upgrade) is in
  progress"** — an interrupted install/upgrade left the release wedged in
  `pending-install`/`pending-upgrade`. All releases set `cleanup_on_fail =
  true`, so this should be rare, but if it happens, uninstall the stuck
  release and re-apply:

  ```bash
  helm uninstall runai-backend -n runai-backend   # or: runai-cluster -n runai
  terraform apply
  ```

- **"HAProxy LoadBalancer IP is not ready yet"** — the ingress Service has no
  external IP yet (or the cloud controller assigned a hostname instead of an
  IP). The plan emits a `check`-block warning and preconditions stop the apply
  before the TLS cert and control-plane release are touched; this never blocks
  `terraform destroy`. Wait for the LB IP and re-run `terraform apply`.

- **Admin password rotation** — the chart consumes `runai_admin_password`
  **only at first install**; changing the variable later and re-applying is a
  silent no-op. Rotate in the Run:ai UI instead. Note that a reinstall
  resurrects the password from tfvars/state, so keep that value in sync with
  the UI.

- **NGC API key rotation** — the helm provider re-resolves the NGC chart repo
  at every plan, so rotate in this order: create the new key → update
  `TF_VAR_ngc_api_key` → `terraform apply` (this also refreshes the
  `runai-reg-creds` pull secret) → verify image pulls → revoke the old key.

- **Bumping `runai_version` misbehaves or leaves CRDs behind** — helm never
  upgrades CRDs on `helm upgrade`. When moving to a new Run:ai version, apply
  the CRDs from the new chart manually first (`kubectl apply -f crds/` from
  the chart source) before `terraform apply`.

## Destroy

`terraform destroy` removes the mk8s cluster, the node groups, and all helm
releases — including **all Run:ai data** (the control-plane Postgres PVC:
users, projects, quotas, audit history, cluster registrations). This is
unrecoverable without backups, and backups are the operator's responsibility —
see the Run:ai backup documentation before destroying (or upgrading). Do not
rely on the default StorageClass reclaim policy as a backup.

Destroy does **not** remove the resources `environment.sh` created outside
Terraform. Clean those up separately:

1. **State bucket** (`tfstate-runai-<hash>`): it is versioned, so every
   historical state version — each containing the TLS key, admin password, and
   NGC key — survives the destroy. Purge it in the console (Object Storage →
   the bucket → delete, after removing all object versions) or via CLI, e.g.
   `s5cmd rm --all-versions "s3://<bucket>/*"` followed by
   `nebius storage bucket delete --name <bucket> --parent-id <project> --ttl 0`.
2. **`runai-sa` service account**: remove its `editors` group membership
   (console: IAM → Groups → editors → Members, or
   `nebius iam group-membership delete`), then delete the SA itself
   (`nebius iam service-account delete --id <id>`).
3. **Access keys**: delete stale `runai-tfstate-*` keys (console: IAM →
   Access keys, or `nebius iam v2 access-key delete --id <id>`).

**After destroying a self-signed deployment, treat the old UI URL as burned:**
the released LB IP can be reassigned to another tenant while
`runai.<ip>.sslip.io` keeps resolving — update bookmarks, docs, and any phase-2
`runai_control_plane_url` values.
