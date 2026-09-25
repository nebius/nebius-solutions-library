# OSMO tf-deploy

This directory is a standalone Terraform-first workflow for OSMO. It does not depend on anything under `applications/osmo/deploy`.

## Prerequisites

- Nebius CLI installed and authenticated for the target project.
- For the default public DNS flow (`osmo.eu-north1.nebius.cloud`), `npc --profile prod` must be installed and authenticated on the machine running step 4:

```bash
npc --profile prod iam whoami
```

- If you are using a different delegated zone or `npc` profile than the default prod setup, see the Hostnames section before step 4.
- If you set `OSMO_BASE_DOMAIN=nip.io`, step 4 can run without `npc`. In that fallback mode the deployment keeps auth and TLS enabled, but skips Nebius SSO registration and relies on the local Keycloak breakglass user.

## Quick Start

For hostname, `npc`, and TLS variants, see the Hostnames and Operational Notes sections below.

1. Initialize the environment:
```bash
cd applications/osmo/tf-deploy
export NEBIUS_TENANT_ID="tenant-XXX"
export NEBIUS_PROJECT_ID="project-XXX"
export NEBIUS_REGION="eu-north1"
export CERT_MANAGER_EMAIL="you@example.com"
# Optional OSMO overrides:
# export OSMO_IMAGE_TAG="6.2"
# export OSMO_CHART_VERSION="1.2.1"
source ./nebius-env-init.sh
```

2. Optional: copy the infra overrides template:
```bash
cp ./infra/terraform.tfvars.example ./infra/terraform.tfvars
```

3. Deploy infra:
```bash
terraform -chdir=./infra init
terraform -chdir=./infra apply
```

4. Prepare app inputs:
```bash
./prepare-app-prereqs.sh
```

5. Deploy the app:
```bash
source ./nebius-env-init.sh
source ./osmo-sso.env
terraform init
terraform apply
```

6. Verify:
```bash
nebius mk8s cluster get-credentials --id "$(terraform -chdir=./infra output -raw cluster_id)" --external
./scripts/verify-installation.sh
terraform -chdir=./ output -raw service_base_url
```

## Layout

- [infra](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/infra): Nebius infra Terraform root. Creates storage, managed PostgreSQL, optional filestore/container registry, and the MK8s cluster.
- [main.tf](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/main.tf), [providers.tf](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/providers.tf), [variables.tf](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/variables.tf), and [outputs.tf](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/outputs.tf): Thin root module. It owns provider configuration, the wrapper module call, and stable outputs.
- [stack](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/stack): Application Terraform child module. The files here stay split by concern:
  - [infra-state.tf](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/stack/infra-state.tf), [app-values.tf](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/stack/app-values.tf), [platform.tf](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/stack/platform.tf), and [secrets.tf](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/stack/secrets.tf): Shared locals, infra-derived values, validation, and shared secrets/TLS resources.
  - [ingress.tf](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/stack/ingress.tf), [osmo.tf](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/stack/osmo.tf), [observability.tf](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/stack/observability.tf), [gpu.tf](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/stack/gpu.tf), [backend-operator.tf](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/stack/backend-operator.tf), [keycloak.tf](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/stack/keycloak.tf), [cert-manager.tf](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/stack/cert-manager.tf), [namespaces.tf](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/stack/namespaces.tf), and [finalize.tf](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/stack/finalize.tf): Concern-based application resources and final configuration hooks.
- [nebius-env-init.sh](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/nebius-env-init.sh): Exports tenant/project/region plus default network/subnet and derived hostnames.
- [prepare-app-prereqs.sh](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/prepare-app-prereqs.sh): Single standalone pre-apply script for kubeconfig sync, Nebius IAM OIDC registration when available, the `nip.io` no-`npc` fallback, and app-secret export materialization for the final Terraform apply. It supports `prepare`, `sync-kubeconfig`, and `register-oidc`.
- [scripts/bootstrap-keycloak.sh](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/scripts/bootstrap-keycloak.sh): Terraform-invoked Keycloak realm/client bootstrap script.
- [scripts/post-install.sh](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/scripts/post-install.sh): Single Terraform-invoked finalize script. Terraform calls it twice: once for post-install fixups and once for OSMO application configuration.
- [scripts/prepare-backend-token.sh](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/scripts/prepare-backend-token.sh): Terraform-invoked helper that reuses or creates the backend operator token for `enable_auth = false`.
- [scripts/verify-installation.sh](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/scripts/verify-installation.sh): Manual post-deploy verification script with a pass/fail summary for cluster health, ingress, OSMO config, storage wiring, backend/operator state, and GPU platform checks.
- [config/helm](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/config/helm): Helm values used by the integrated application root for observability and GPU infrastructure.
- [config/osmo](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/config/osmo): OSMO pod-template and GPU platform JSON payloads used during the final application-configuration step.
- [config/keycloak](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/config/keycloak): Keycloak realm bootstrap payloads.

## What Infra Creates

The [infra](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/infra) root is copied into `tf-deploy` from the example IaC flow and is now independent of `deploy`. It creates:

- Nebius managed Kubernetes cluster and node groups
- Nebius managed PostgreSQL
- S3-compatible object storage bucket and access key
- optional filestore
- optional container registry
- optional WireGuard

It still expects an existing default Nebius VPC network and subnet in the project. [nebius-env-init.sh](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/nebius-env-init.sh) discovers those and exports `TF_VAR_network_id` / `TF_VAR_subnet_id`.

## Hostnames

The default mode is the delegated prod DNS zone: `osmo.eu-north1.nebius.cloud`, with `cert-manager` handling public TLS.

For that default zone, [nebius-env-init.sh](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/nebius-env-init.sh) now also defaults:

- `DNS_NPC_PROFILE=prod`
- `DNS_ZONE_ID=dnszone-e00gx67zvqhjmpmd6m`

On a fresh install, you can leave `OSMO_INGRESS_HOSTNAME` and `KEYCLOAK_HOSTNAME` unset. With the default base domain, step 4 will:

- derive stable hostnames from the project id suffix
- bootstrap ingress-nginx
- read the public LoadBalancer IP
- print the public DNS records required for OSMO and Keycloak
- write the final hostnames into [osmo-sso.env](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/osmo-sso.env) for step 5

If you want short-lived testing with `nip.io`, set:

- `OSMO_BASE_DOMAIN=nip.io`

In that mode, step 4 will:

- bootstrap ingress-nginx
- read the public LoadBalancer IP
- derive final hostnames such as `osmo.<ip>.nip.io` and `auth-osmo.<ip>.nip.io`
- write them into [osmo-sso.env](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/osmo-sso.env) for step 5

If `npc` is installed, step 4 also registers or updates Nebius SSO for those hostnames. If `npc` is not installed, step 4 writes `TF_VAR_nebius_sso_enabled=false` into [osmo-sso.env](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/osmo-sso.env) so the final apply uses the local Keycloak breakglass user instead.

This `nip.io` fallback does not require `tls_mode = "self-signed"`. The default `tls_mode = "cert-manager"` still works because the derived `osmo.<ip>.nip.io` and `auth-osmo.<ip>.nip.io` hostnames resolve publicly once the ingress IP is known, so ACME HTTP-01 can succeed without `npc`-managed DNS. Keep `CERT_MANAGER_EMAIL` or `TF_VAR_cert_manager_email` set to a real email address when using this path. Use `self-signed` only if you intentionally want to skip public certificate issuance.

If you already know the ingress IP or are rerunning against an existing cluster, you can set explicit `nip.io` hostnames such as:

- `OSMO_INGRESS_HOSTNAME=osmo.89.169.122.98.nip.io`
- `KEYCLOAK_HOSTNAME=auth-osmo.89.169.122.98.nip.io`

If you own a real DNS zone, [nebius-env-init.sh](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/nebius-env-init.sh) can still derive:

- `OSMO_INGRESS_HOSTNAME=osmo-<project-id-suffix>.<OSMO_BASE_DOMAIN>`
- `KEYCLOAK_HOSTNAME=auth-osmo-<project-id-suffix>.<OSMO_BASE_DOMAIN>`

For example, with:

- `NEBIUS_PROJECT_ID=project-e00ssrmcpr00kjfkyr82j8`
- `OSMO_BASE_DOMAIN=osmo.eu-north1.nebius.cloud`

the derived hostnames are:

- `osmo-e00ssrmcpr00kjfkyr82j8.osmo.eu-north1.nebius.cloud`
- `auth-osmo-e00ssrmcpr00kjfkyr82j8.osmo.eu-north1.nebius.cloud`

Keep these hostnames stable. The OIDC redirect URI uses the Keycloak hostname:

`https://$KEYCLOAK_HOSTNAME/realms/osmo/broker/nebius-sso/endpoint`

On a fresh install with a real DNS zone, step 4 bootstraps ingress-nginx, discovers the public LoadBalancer IP, and attempts to upsert the exact public `A` records needed for those hostnames. It does that with `npc --profile "$DNS_NPC_PROFILE" dns infra record upsert-recordset ...`. If `DNS_ZONE_ID` and `DNS_NPC_PROFILE` are not set for your zone, it falls back to printing the records you need before the final app apply when using `tls_mode = "cert-manager"`. The final app Terraform root now also reconciles those same recordsets when `dns_base_domain`, `dns_zone_id`, and `dns_npc_profile` are set, so `terraform destroy` can remove them cleanly later.

If your team owns the delegated zone, use the public Infra DNS API to manage those records:

```bash
npc --profile prod dns infra record create \
  --parent-id <dns-zone-id> \
  --relative-name osmo-<project-id-suffix> \
  --type a \
  --data <ingress-lb-ip> \
  --ttl 300

npc --profile prod dns infra record create \
  --parent-id <dns-zone-id> \
  --relative-name auth-osmo-<project-id-suffix> \
  --type a \
  --data <ingress-lb-ip> \
  --ttl 300
```

You can also manage the zone and records in Terraform with `nebius_dns_infra_v1_zone` and `nebius_dns_infra_v1_record`. Do not use the deprecated `nebius_dns_v1alpha1_*` resources for new specs, and do not rely on the internal `dns inner ... maintenance upsert-recordset` command unless you are on the DNS team.

## Generated Env Files

This workflow writes a few local env files. Do not commit them.

- [osmo-sso.env](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/osmo-sso.env): Nebius IAM OIDC client ID and secret when Nebius SSO is enabled, or the `nip.io` fallback exports that disable Nebius SSO when `npc` is unavailable; in both cases it also carries the final OSMO and Keycloak hostnames, the `TF_VAR_*` secret overrides that the final app apply uses for PostgreSQL/storage, and on self-signed fallback installs the OSMO CLI trust exports written after step 5.
- [cluster-access.env](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/cluster-access.env): kubeconfig path and context for the new cluster.

## Operational Notes

- The app Terraform root reads PostgreSQL and storage values directly from [infra](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/infra) state. There are no separate wrapper scripts for Keycloak bootstrap or stack-env rendering anymore.
- `source ./nebius-env-init.sh && source ./osmo-sso.env` before the final `terraform apply` so Terraform has Nebius auth plus the generated SSO inputs in the current shell.
- Keep the Nebius auth environment from step 1 in the same shell and rerun `terraform init` after pulling changes. If step 4 succeeds normally, the final app apply will use the exported `TF_VAR_*` secret overrides instead of re-reading the storage payload through Terraform.
- `prepare-app-prereqs.sh` now resolves the storage secret with the Nebius CLI during step 4 and writes it into [osmo-sso.env](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/osmo-sso.env), so a normal step 5 does not need Terraform itself to read that payload from MysteryBox.
- On the `nip.io` no-`npc` fallback, the final app apply still bootstraps Keycloak and creates the local `osmo-admin` breakglass user; Nebius SSO is the only part that is skipped.
- The machine running step 5 must be able to resolve and reach the MK8s public endpoint from the generated kubeconfig.
- [prepare-app-prereqs.sh](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/prepare-app-prereqs.sh) is safe to rerun. It rewrites kubeconfig outputs and force-replaces the Nebius OIDC client secret, so if you rerun step 4, source [osmo-sso.env](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/osmo-sso.env) again and rerun step 5.
- Run [scripts/verify-installation.sh](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/scripts/verify-installation.sh) after step 5 for a manual acceptance pass. It is intentionally separate from Terraform so you can rerun it after fixes without tainting Terraform state.

## Advanced Notes

- On self-signed fallback installs, step 5 also updates [osmo-sso.env](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/osmo-sso.env) with `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, and `SSL_CERT_FILE` pointing at `generated/osmo-cli-ca.pem`. Re-source the same file after `terraform apply` before using `osmo login` or `osmo dataset ...`.
- If you ran step 4 but never reached a successful final app state, you can delete the managed public DNS recordsets manually with:
  `./prepare-app-prereqs.sh cleanup-dns`
- [prepare-app-prereqs.sh](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/prepare-app-prereqs.sh) still writes [cluster-access.env](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/cluster-access.env) for direct `kubectl` use, but the app Terraform root defaults to `./generated/kubeconfig`.
- If you are using `nip.io`, do not rely on `osmo-<project-id>.nip.io`; `nip.io` must encode the ingress IP in the hostname. Step 4 handles this automatically on a fresh install.
- The application root now defaults to `tls_mode = "cert-manager"` for public `osmo.eu-north1.nebius.cloud` hostnames. Set `cert_manager_email`, use stable public hostnames, and make sure those names resolve publicly before the final app apply. `self-signed` remains available as an explicit fallback.
- `tf-deploy` intentionally does not implement the example’s interactive `certbot` DNS-01 path. The supported Terraform-native TLS modes are managed `cert-manager` and explicit `self-signed` fallback.
- When auth is enabled, the app root adds in-cluster hostname aliases for the OSMO and Keycloak public hostnames so the sidecars can reach ingress before public DNS is in place.
- The application root owns ingress-nginx and Keycloak. Keycloak bootstrap runs during the same app apply and uses a temporary port-forward, not the public ingress.
- The final app apply now also covers the functional gaps from `deploy/example`: observability, backend operator, workflow storage, dataset bucket registration, GPU operator and KAI scheduler when GPU nodes exist, and OSMO GPU platform wiring.
- GPU infrastructure auto-enables when the infra state reports GPU node groups. On a CPU-only cluster, the final app apply skips GPU operator, KAI scheduler, backend KAI scheduler settings, and GPU platform configuration.
- The backend operator path is automatic for the default auth-enabled install and uses password login. When `tls_mode = "self-signed"`, `tf-deploy` mounts the self-signed Keycloak certificate into the backend operator pods so they can trust the auth endpoint. If you intentionally set `enable_auth = false`, the integrated app root reuses or creates the `osmo-operator-token` secret for the backend operator. You can still override that path with `backend_operator_login_method = "token"` and an explicit `backend_operator_service_token` in [terraform.tfvars.example](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/terraform.tfvars.example).

## What the App Root Owns

The Terraform root in [applications/osmo/tf-deploy](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy) owns:

- `osmo` namespace
- `monitoring`, `osmo-operator`, `osmo-workflows`, `gpu-operator`, `network-operator`, and `kai-scheduler` namespaces when enabled
- `cert-manager` namespace, Helm release, and `ClusterIssuer` when `tls_mode = "cert-manager"` and `deploy_cert_manager = true`
- public DNS `A` recordsets for the OSMO and Keycloak hostnames when `dns_zone_id` and `dns_npc_profile` are set
- ingress-nginx Helm release and service
- Keycloak Helm release and realm/client bootstrap
- Redis Helm release
- OSMO service/router/UI Helm releases
- kube-prometheus-stack, Loki, and Promtail when `deploy_observability=true`
- OSMO backend operator when `deploy_backend_operator=true`
- NVIDIA GPU Operator, optional NVIDIA Network Operator, and KAI scheduler when GPU support is enabled
- secrets/configmaps needed by OSMO
- post-install fixes and final application configuration via [scripts/post-install.sh](/Users/timothyle/repos/nebius-solutions-library/applications/osmo/tf-deploy/scripts/post-install.sh)

## Connecting a Remote Kubernetes Cluster

You can connect additional Kubernetes clusters to a running OSMO deployment as remote backends. The OSMO control plane (service, router, UI, Keycloak, PostgreSQL, Redis) stays on the primary cluster. The remote cluster runs only the backend operator, which registers itself with OSMO and executes workflow pods locally.

### Prerequisites

- A running OSMO deployment (steps 1-6 above completed).
- The OSMO CLI authenticated against the primary deployment.
- `kubectl` access to the remote cluster.
- `helm` v3.
- `jq`.
- The OSMO service URL (the public hostname, e.g. `https://osmo-e00repdwpr0008kymg29xe.osmo.eu-north1.nebius.cloud`).
- The remote cluster must be able to reach the OSMO service URL over the network.
- Use an OSMO CLI version that matches the primary deployment closely. If the server advertises a newer 6.2.x client, update the CLI before creating the token.
- If you use a kubeconfig generated by `sky`, make sure the `sky-kube-exec-wrapper` credential plugin is installed on the machine running `kubectl` and `helm`. If that plugin is unavailable, regenerate cluster credentials with `nebius mk8s cluster get-credentials --id <cluster-id> --external` and use that kubeconfig instead.

### 1. Set the placeholders and log into OSMO

On a machine with the OSMO CLI authenticated against the primary deployment, set these values first:

```bash
export OSMO_HOSTNAME="<osmo-public-hostname>"
export REMOTE_BACKEND_NAME="<unique-remote-backend-name>"
export REMOTE_BACKEND_USER="backend-${REMOTE_BACKEND_NAME}"
export REMOTE_POOL_NAME="${REMOTE_BACKEND_NAME}"
export REMOTE_POOL_ROLE="osmo-${REMOTE_POOL_NAME}"
export OSMO_USER_ID="<your-osmo-user-id-or-email>"
export BACKEND_CLUSTER_ID="<remote-mk8s-cluster-id>"
export BACKEND_KUBECONFIG="$HOME/.kube/<remote-backend-name>.yaml"
export BACKEND_WORKFLOWS_NAMESPACE="osmo-workflows"
export OSMO_BACKEND_OPERATOR_CHART_VERSION="1.2.1"
export OSMO_IMAGE_TAG="6.2"

osmo login "https://${OSMO_HOSTNAME}"
```

`REMOTE_BACKEND_NAME` must be unique across OSMO backends and should not be `default`.

### 2. Create a backend service account and token

```bash
osmo user create "${REMOTE_BACKEND_USER}" --roles osmo-backend

export REMOTE_SERVICE_TOKEN="$(
  osmo token set "remote-backend-token-$(date +%s)" \
    --user "${REMOTE_BACKEND_USER}" \
    --roles osmo-backend \
    --expires-at 2027-01-01 \
    --description "Backend operator token for ${REMOTE_BACKEND_NAME}" \
    -t json | jq -r '.token'
)"
```

If the backend service account already exists, skip the `osmo user create` step and mint a new token for the existing user.

### 3. Generate a kubeconfig for the remote cluster

If you already have a working kubeconfig for the remote cluster, point `BACKEND_KUBECONFIG` at that file and skip this step. Otherwise generate one with Nebius CLI:

```bash
nebius mk8s cluster get-credentials \
  --id "${BACKEND_CLUSTER_ID}" \
  --external \
  --kubeconfig "${BACKEND_KUBECONFIG}" \
  --force

KUBECONFIG="${BACKEND_KUBECONFIG}" kubectl config current-context
```

### 4. Prepare the remote cluster

Create the namespaces and token secret on the remote cluster:

```bash
KUBECONFIG="${BACKEND_KUBECONFIG}" kubectl create namespace osmo-operator --dry-run=client -o yaml | \
  KUBECONFIG="${BACKEND_KUBECONFIG}" kubectl apply -f -

KUBECONFIG="${BACKEND_KUBECONFIG}" kubectl create namespace osmo-workflows --dry-run=client -o yaml | \
  KUBECONFIG="${BACKEND_KUBECONFIG}" kubectl apply -f -

KUBECONFIG="${BACKEND_KUBECONFIG}" kubectl label namespace osmo-operator \
  pod-security.kubernetes.io/enforce=privileged --overwrite

KUBECONFIG="${BACKEND_KUBECONFIG}" kubectl label namespace osmo-workflows \
  pod-security.kubernetes.io/enforce=privileged --overwrite

KUBECONFIG="${BACKEND_KUBECONFIG}" kubectl create secret generic osmo-operator-token \
  -n osmo-operator \
  --from-literal=token="${REMOTE_SERVICE_TOKEN}" \
  --dry-run=client -o yaml | \
  KUBECONFIG="${BACKEND_KUBECONFIG}" kubectl apply -f -
```

### 5. Install the backend operator

```bash
cat > /tmp/backend_operator_values.yaml <<EOF
global:
  serviceUrl: "https://${OSMO_HOSTNAME}"
  loginMethod: token
  accountTokenSecret: osmo-operator-token
  accountTokenSecretKey: token
  backendName: "${REMOTE_BACKEND_NAME}"
  backendNamespace: osmo-workflows
  agentNamespace: osmo-operator
  osmoImageTag: "${OSMO_IMAGE_TAG}"

agent:
  securityContext:
    privileged: true
  hostNetwork: true
  hostPID: true

podMonitor:
  enabled: false

services:
  backendListener:
    volumes:
    - name: progress-files
      emptyDir: {}
    volumeMounts:
    - name: progress-files
      mountPath: /var/run/osmo

  backendWorker:
    volumes:
    - name: progress-files
      emptyDir: {}
    volumeMounts:
    - name: progress-files
      mountPath: /var/run/osmo

backendTestRunner:
  enabled: false
EOF

helm repo add osmo https://helm.ngc.nvidia.com/nvidia/osmo --force-update
helm repo update

KUBECONFIG="${BACKEND_KUBECONFIG}" helm upgrade --install osmo-operator osmo/backend-operator \
  -f /tmp/backend_operator_values.yaml \
  --namespace osmo-operator \
  --version "${OSMO_BACKEND_OPERATOR_CHART_VERSION}" \
  --wait \
  --timeout 10m
```

The `progress-files` `emptyDir` keeps the backend operator progress files under `/var/run/osmo` across container restarts, which avoids startup probe loops on clusters where the default chart values are insufficient.

### 6. Create a dedicated pool for the remote backend

For an additional cluster, do not repoint the existing `default` pool away from the primary backend. Create a dedicated pool instead:

```bash
osmo config show POOL > /tmp/current-pools.json

jq \
  --arg pool "${REMOTE_POOL_NAME}" \
  --arg backend "${REMOTE_BACKEND_NAME}" \
  '.pools = ((.pools // {}) + {
    ($pool): {
      name: $pool,
      backend: $backend,
      description: ("Remote pool for " + $backend)
    }
  })' \
  /tmp/current-pools.json > /tmp/remote-pools.json

osmo config update POOL \
  --file /tmp/remote-pools.json \
  --description "Create pool ${REMOTE_POOL_NAME} for ${REMOTE_BACKEND_NAME}"

EDITOR=/usr/bin/true osmo config set ROLE "${REMOTE_POOL_ROLE}" pool \
  --description "Create pool role ${REMOTE_POOL_ROLE}"

osmo user update "${OSMO_USER_ID}" --add-roles "${REMOTE_POOL_ROLE}"
```

Use your human OSMO user id or email for `OSMO_USER_ID`, not the backend service account.

If your OSMO CLI errors with `Editor not found` on `osmo config set ROLE`, keep the same command and prefix it with `EDITOR=/usr/bin/true` to force a non-interactive run. On current clients, also pass `--description` or the command may abort due to an empty description.

### 7. Verify registration

After a few minutes the backend operator should register with OSMO:

```bash
KUBECONFIG="${BACKEND_KUBECONFIG}" kubectl get pods -n osmo-operator -o wide
osmo config show BACKEND
osmo config show POOL "${REMOTE_POOL_NAME}"
osmo pool list
```

Success looks like this:

- `osmo-operator-osmo-backend-listener` and `osmo-operator-osmo-backend-worker` are `Running` on the remote cluster.
- `osmo config show BACKEND` includes `${REMOTE_BACKEND_NAME}` with `online: true`.
- `osmo config show POOL "${REMOTE_POOL_NAME}"` reports `status: "ONLINE"`.

### 8. GPU support (optional)

If the remote cluster has GPU nodes, install the GPU operator and KAI scheduler before submitting GPU workflows:

```bash
export GPU_OPERATOR_CHART_VERSION="<gpu-operator-chart-version>"  # for example: v26.3.0

helm repo add nvidia https://helm.ngc.nvidia.com/nvidia --force-update
helm repo update

KUBECONFIG="${BACKEND_KUBECONFIG}" helm upgrade --install gpu-operator nvidia/gpu-operator \
  --version "${GPU_OPERATOR_CHART_VERSION}" \
  --namespace gpu-operator --create-namespace \
  --skip-crds \
  --set driver.enabled=false \
  --set nfd.enabled=false \
  --set devicePlugin.enabled=false \
  --set gfd.enabled=false \
  --set dcgm.enabled=false \
  --set dcgmExporter.enabled=false \
  --set nodeStatusExporter.enabled=false \
  --set ccManager.enabled=false \
  --set sandboxDevicePlugin.enabled=false

KUBECONFIG="${BACKEND_KUBECONFIG}" helm upgrade --install kai-scheduler \
  oci://ghcr.io/nvidia/kai-scheduler/kai-scheduler \
  --version v0.12.4 \
  --namespace kai-scheduler --create-namespace
```

Verify that the remote cluster has both the KAI scheduler and the NVIDIA runtime class before submitting workflows:

```bash
KUBECONFIG="${BACKEND_KUBECONFIG}" kubectl get crd | grep -i podgroup
KUBECONFIG="${BACKEND_KUBECONFIG}" kubectl get pods -n kai-scheduler -o wide
KUBECONFIG="${BACKEND_KUBECONFIG}" kubectl get runtimeclass
KUBECONFIG="${BACKEND_KUBECONFIG}" kubectl get pods -n gpu-operator -o wide
KUBECONFIG="${BACKEND_KUBECONFIG}" kubectl get nodes \
  -o custom-columns=NAME:.metadata.name,GPU_LABEL:.metadata.labels.nvidia\\.com/gpu\\.present,GPU_ALLOC:.status.allocatable.nvidia\\.com/gpu
```

If the `podgroups.scheduling.run.ai` CRD is missing or the `kai-scheduler` pods are absent, OSMO workflows may fail immediately with `FAILED_SERVER_ERROR` and a `ResourceNotFoundError` for `PodGroup`.

If the `nvidia` `RuntimeClass` is missing, OSMO GPU workflows may fail immediately with `FAILED_SERVER_ERROR` and `pod rejected: RuntimeClass "nvidia" not found`.

If a GPU node is labeled with `nvidia.com/gpu.present=true` but `nvidia.com/gpu` is still missing from the node's allocatable resources, the cluster still does not have a working device plugin. In that case, enable the actual device plugin in the GPU operator and re-check the nodes:

```bash
KUBECONFIG="${BACKEND_KUBECONFIG}" helm upgrade gpu-operator nvidia/gpu-operator \
  --version "${GPU_OPERATOR_CHART_VERSION}" \
  --namespace gpu-operator \
  --skip-crds \
  --set driver.enabled=false \
  --set nfd.enabled=false \
  --set devicePlugin.enabled=true \
  --set gfd.enabled=false \
  --set dcgm.enabled=false \
  --set dcgmExporter.enabled=false \
  --set nodeStatusExporter.enabled=false \
  --set ccManager.enabled=false \
  --set sandboxDevicePlugin.enabled=false

KUBECONFIG="${BACKEND_KUBECONFIG}" kubectl get nodes \
  -o custom-columns=NAME:.metadata.name,GPU_LABEL:.metadata.labels.nvidia\\.com/gpu\\.present,GPU_ALLOC:.status.allocatable.nvidia\\.com/gpu
```

Some managed clusters already ship a release or namespace named `nvidia-device-plugin` that only deploys GPU feature discovery or Node Feature Discovery. Do not assume that means the actual Kubernetes device plugin is present. The decisive signal is whether the nodes advertise `nvidia.com/gpu` in allocatable resources.

Then update the backend's scheduler settings in OSMO so the remote backend uses KAI. The safe helper in this repo preserves the backend's existing router and listener settings:

```bash
# Point kubectl at the primary OSMO cluster before running this helper.
OSMO_BACKEND_NAME="${REMOTE_BACKEND_NAME}" \
  ../deploy/example/002-setup/10-configure-backend-scheduler.sh
```

Then create a KAI queue for the remote OSMO pool. OSMO labels KAI workloads with a queue name in the form `osmo-pool-<workflows-namespace>-<pool-name>`. If that queue does not exist, or if it has zero non-preemptible quota, workflows can sit in `SCHEDULING` with a `NonPreemptibleOverQuota` reason even when GPU nodes are healthy:

```bash
export REMOTE_KAI_PARENT_QUEUE="default-parent-queue"
export REMOTE_KAI_QUEUE="osmo-pool-${BACKEND_WORKFLOWS_NAMESPACE}-${REMOTE_POOL_NAME}"
export REMOTE_KAI_CPU_QUOTA_MILLICORES="<cpu-millicores-quota>"
export REMOTE_KAI_GPU_QUOTA="<gpu-quota>"
export REMOTE_KAI_MEMORY_QUOTA_MB="<memory-megabytes-quota>"

cat > /tmp/remote-kai-queues.yaml <<EOF
apiVersion: scheduling.run.ai/v2
kind: Queue
metadata:
  name: ${REMOTE_KAI_PARENT_QUEUE}
  annotations:
    helm.sh/resource-policy: keep
spec:
  resources:
    cpu:
      quota: ${REMOTE_KAI_CPU_QUOTA_MILLICORES}
      limit: -1
      overQuotaWeight: 1
    gpu:
      quota: ${REMOTE_KAI_GPU_QUOTA}
      limit: -1
      overQuotaWeight: 1
    memory:
      quota: ${REMOTE_KAI_MEMORY_QUOTA_MB}
      limit: -1
      overQuotaWeight: 1
---
apiVersion: scheduling.run.ai/v2
kind: Queue
metadata:
  name: ${REMOTE_KAI_QUEUE}
spec:
  parentQueue: ${REMOTE_KAI_PARENT_QUEUE}
  resources:
    cpu:
      quota: ${REMOTE_KAI_CPU_QUOTA_MILLICORES}
      limit: -1
      overQuotaWeight: 1
    gpu:
      quota: ${REMOTE_KAI_GPU_QUOTA}
      limit: -1
      overQuotaWeight: 1
    memory:
      quota: ${REMOTE_KAI_MEMORY_QUOTA_MB}
      limit: -1
      overQuotaWeight: 1
EOF

KUBECONFIG="${BACKEND_KUBECONFIG}" kubectl apply -f /tmp/remote-kai-queues.yaml
KUBECONFIG="${BACKEND_KUBECONFIG}" kubectl get queue "${REMOTE_KAI_PARENT_QUEUE}" "${REMOTE_KAI_QUEUE}" -o yaml
```

Set the KAI quotas large enough for the remote pool. For a dedicated single-node GPU backend, matching the node's allocatable CPU, GPU, and memory is usually the simplest choice.

If you want workflows to use `platform: gpu` on the remote pool, first inspect a working GPU-enabled pool to find its platform name. `tf-deploy` usually creates a friendly platform key such as `H100`, `H200`, or `L40S`, not necessarily a literal `gpu` key:

```bash
export SOURCE_GPU_POOL="<existing-gpu-enabled-pool>"

osmo config show POOL "${SOURCE_GPU_POOL}" > /tmp/source-gpu-pool.json
jq -r '.platforms | keys[]' /tmp/source-gpu-pool.json
```

Then copy that source platform config onto the remote pool under the `gpu` alias:

```bash
export SOURCE_PLATFORM_NAME="<existing-platform-name>"

osmo config show POOL "${REMOTE_POOL_NAME}" > /tmp/remote-pool.json

jq \
  --slurpfile source /tmp/source-gpu-pool.json \
  --arg source_platform "${SOURCE_PLATFORM_NAME}" \
  '.platforms = ((.platforms // {}) + {
    gpu: ($source[0].platforms[$source_platform])
  })' \
  /tmp/remote-pool.json > /tmp/remote-pool-with-gpu.json

osmo config update POOL "${REMOTE_POOL_NAME}" \
  --file /tmp/remote-pool-with-gpu.json \
  --description "Add gpu platform alias to ${REMOTE_POOL_NAME}"
```

After that, a workflow resource block with `platform: gpu` will resolve on the remote pool the same way it does on the source GPU pool.

### 9. Storage access (optional)

If workflows on the remote cluster need to read or write OSMO datasets, create the storage credentials secret in the workflows namespace:

```bash
KUBECONFIG="${BACKEND_KUBECONFIG}" kubectl create secret generic osmo-storage \
  -n osmo-workflows \
  --from-literal=access-key-id="<s3-access-key-id>" \
  --from-literal=secret-access-key="<s3-secret-access-key>"
```

### Removing a remote backend

To disconnect a remote cluster:

```bash
KUBECONFIG="${BACKEND_KUBECONFIG}" helm uninstall osmo-operator -n osmo-operator
KUBECONFIG="${BACKEND_KUBECONFIG}" kubectl delete namespace osmo-operator osmo-workflows
osmo config delete POOL "${REMOTE_POOL_NAME}" --description "Remove remote pool ${REMOTE_POOL_NAME}"
osmo config delete ROLE "${REMOTE_POOL_ROLE}" --description "Remove remote pool role ${REMOTE_POOL_ROLE}"
```

The backend will go offline in OSMO after its heartbeat expires (a few minutes).
