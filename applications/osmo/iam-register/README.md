## OIDC client registration for Nebius SSO (Keycloak / OSMO)

This folder contains a template and steps to register an **external OIDC client** in Nebius IAM using `npc`, then wire it into Keycloak as the Nebius SSO IdP for OSMO.

### 1. Prerequisites

- `npc` CLI configured (`npc iam whoami` works).
- Project NID (your Nebius project ID).
- The external hostname where **Keycloak** will be exposed.
  - **Recommended (no /etc/hosts):** use **nip.io** so the hostname resolves everywhere (browser and cluster). Example: `osmo.89-169-122-246.nip.io` and `auth-osmo.89-169-122-246.nip.io` (replace with your LoadBalancer IP in dashed form). See [Hostnames without /etc/hosts](#hostnames-without-etchosts) below.
  - Or set `KEYCLOAK_HOSTNAME` / `OSMO_INGRESS_HOSTNAME` to a domain you control (DNS A record to the LoadBalancer IP).

### 2. Fill in `oidc-client.yaml`

Edit `oidc-client.yaml` and replace:

- `project.<YOUR_PROJECT_NID>` with your actual project NID.
  - You can get it from your existing Terraform/ENV (`NEBIUS_PROJECT_ID`), or via:
    - `npc iam project list --format table` and pick the appropriate `ID`.
- The redirect URI in `oidc-client.yaml` must match your `KEYCLOAK_HOSTNAME`. The template uses nip.io (e.g. `auth-osmo.89-169-122-246.nip.io`); replace the IP part with your LoadBalancer IP if different.

Do **not** add a trailing slash to the redirect URI; keep the path exactly as in the template.

### 3. Create the OIDC client

From this folder:

```bash
npc iam oidc-client create --file oidc-client.yaml --format yaml
```

Save the returned `client_id` (OIDC client NID).

### 4. Generate the client secret

```bash
npc iam oidc-client generate-client-secret \
  --client-id <CLIENT_ID_FROM_CREATE> \
  --format yaml
```

Save the returned `client_secret` securely (it’s shown only once).

### 4a. Auto-create OIDC client (04-deploy)

**04-deploy-osmo-control-plane.sh** can create the Nebius IAM OIDC client for you when `NEBIUS_SSO_CLIENT_ID` and `NEBIUS_SSO_CLIENT_SECRET` are not set: it runs `npc iam oidc-client create` (and generates the secret) if `npc` is in PATH and `NEBIUS_PROJECT_ID` is set (e.g. after `source 000-prerequisites/nebius-env-init.sh`). Add the returned `NEBIUS_SSO_CLIENT_ID` to `osmo-deploy.env` for future runs.

### 4b. Update redirect URI on an existing client (per-cluster or when switching hostname)

**04-deploy-osmo-control-plane.sh** runs `npc iam oidc-client update` automatically when `npc` is installed and `NEBIUS_SSO_CLIENT_ID` is set, so the redirect URI for the current cluster is set each time you deploy. If `npc` is not available or the update fails, run the command below manually.

If you already have an OIDC client (e.g. with `auth-osmo.local`) and are switching to nip.io or another hostname, update the redirect URI instead of creating a new client:

```bash
# Replace <CLIENT_ID> with your client id (e.g. oidcclient-e00p22c4ck7cgkqt2r)
# Replace the URL with your Keycloak callback URL (must match KEYCLOAK_HOSTNAME)
# --authorization-grant-types is required with --patch (API rejects empty grant types)
npc iam oidc-client update <CLIENT_ID> \
  --redirect-uris "https://auth-osmo.89-169-122-246.nip.io/realms/osmo/broker/nebius-sso/endpoint" \
  --authorization-grant-types authorization_code \
  --patch \
  --format yaml
```

**Reuse your existing credentials:** The client secret does not change when you only update the redirect URI. Keep using the same `NEBIUS_SSO_CLIENT_ID`, `NEBIUS_SSO_CLIENT_SECRET`, and `OSMO_POSTGRESQL_PASSWORD` (and any other deploy env) you had before; no need to regenerate the client secret.

### 5. Configure OSMO / Keycloak

Either export env vars before running `04-deploy-osmo-control-plane.sh`:

```bash
export NEBIUS_SSO_ENABLED=true
export NEBIUS_SSO_ISSUER_URL="https://auth.nebius.com"
export NEBIUS_SSO_CLIENT_ID="<CLIENT_ID_FROM_CREATE>"
export NEBIUS_SSO_CLIENT_SECRET="<CLIENT_SECRET_FROM_GENERATE>"
```

Or store the secret in Kubernetes and only set the client ID:

```bash
kubectl create secret generic keycloak-nebius-sso-secret \
  -n osmo \
  --from-literal=client_secret="<CLIENT_SECRET_FROM_GENERATE>"

export NEBIUS_SSO_ENABLED=true
export NEBIUS_SSO_ISSUER_URL="https://auth.nebius.com"
export NEBIUS_SSO_CLIENT_ID="<CLIENT_ID_FROM_CREATE>"
```

Then re-run:

```bash
cd ../002-setup
./04-deploy-osmo-control-plane.sh
```

Keycloak will be configured to use Nebius SSO (issuer `https://auth.nebius.com`) as the primary IdP for OSMO.

---

## Hostnames without /etc/hosts

So that **pods** (e.g. Envoy JWKS fetch) and your **browser** can reach Keycloak and OSMO without editing `/etc/hosts`, use a real DNS name. Two options:

1. **nip.io (no domain purchase)**  
   [nip.io](https://nip.io) resolves `*.<dashed-ip>.nip.io` to that IP. Example: LoadBalancer IP `89.169.122.246` → use:
   - **OSMO UI/API:** `osmo.89-169-122-246.nip.io`
   - **Keycloak:** `auth-osmo.89-169-122-246.nip.io`  
   Replace the dashed IP with your own (e.g. `1-2-3-4` for `1.2.3.4`). No sign-up; works from inside the cluster and from your machine.

2. **Your own domain**  
   Create A records for e.g. `osmo.example.com` and `auth-osmo.example.com` pointing to the LoadBalancer IP.

Use the same hostnames in `oidc-client.yaml` redirect URI(s), in `KEYCLOAK_HOSTNAME` / `OSMO_INGRESS_HOSTNAME`, and when running the TLS script.

---

## Final steps from the start (nothing run yet)

There is **no shortcut**: you need the cluster, ingress, and then 04. Run in this order.

### A. Prerequisites (once)

From `applications/osmo/deploy/example/000-prerequisites`:

```bash
cd applications/osmo/deploy/example/000-prerequisites
./install-tools.sh --check    # install anything missing
source ./nebius-env-init.sh   # tenant + project
source ./secrets-init.sh      # optional: MysteryBox for DB + MEK
```

### B. Infra and cluster

From `applications/osmo/deploy/example`:

```bash
cd 001-iac
terraform init
terraform apply   # MK8s, PostgreSQL, etc.
```

Get kubeconfig (e.g. `nebius mk8s cluster get-credentials ...` per your docs).

### C. Keycloak hostname and OIDC client registration

Use nip.io (recommended) or add a hostname to `/etc/hosts` and set `KEYCLOAK_HOSTNAME` / `OSMO_INGRESS_HOSTNAME` to match. The redirect URI in Nebius must match your Keycloak host; [update it](#4b-update-redirect-uri-on-an-existing-client-when-switching-hostname) if you change hostnames.

From `applications/osmo/iam-register`:

1. Edit `oidc-client.yaml` if needed: set the redirect URI to match your `KEYCLOAK_HOSTNAME` (template uses nip.io; replace the dashed IP with your LoadBalancer IP). Ensure `parent_id` matches your project NID. Or [update the redirect URI](#4b-update-redirect-uri-on-an-existing-client-when-switching-hostname) on an existing client.
2. Create client and secret (or skip if you already have a client and only updated its redirect URI):

```bash
cd applications/osmo/iam-register
npc iam oidc-client create --file oidc-client.yaml --format yaml
# save client_id

npc iam oidc-client generate-client-secret --client-id <CLIENT_ID> --format yaml
# save client_secret (one-time)
```

3. Export for 04 (use the same shell or add to your env):

```bash
export KEYCLOAK_HOSTNAME="auth-osmo.89-169-122-246.nip.io"   # must match redirect URI host (replace IP with your LB IP)
export OSMO_INGRESS_HOSTNAME="osmo.89-169-122-246.nip.io"
export NEBIUS_SSO_ENABLED=true
export NEBIUS_SSO_ISSUER_URL="https://auth.nebius.com"
export NEBIUS_SSO_CLIENT_ID="<CLIENT_ID>"
export NEBIUS_SSO_CLIENT_SECRET="<CLIENT_SECRET>"
```

If you only [updated the redirect URI](#4b-update-redirect-uri-on-an-existing-client-when-switching-hostname) on an existing client, keep using your existing `NEBIUS_SSO_CLIENT_SECRET` and `OSMO_POSTGRESQL_PASSWORD`; they do not change.

### D. Kubernetes setup and OSMO control plane

From `applications/osmo/deploy/example/002-setup`:

```bash
cd applications/osmo/deploy/example/002-setup

./01-deploy-gpu-infrastructure.sh
./02-deploy-observability.sh
./03-deploy-nginx-ingress.sh
```

Optional but recommended for production: TLS for Keycloak (so redirect URI is https). If you use it, run the TLS script for `KEYCLOAK_HOSTNAME` (e.g. `04-enable-tls.sh` or `03a` per the repo) before 04. If you skip TLS, 04 may still run but auth will be http-only.

Then:

```bash
./04-deploy-osmo-control-plane.sh
```

(04 creates the `osmo` namespace, DB init, secrets, Redis, Keycloak, OSMO, and configures Nebius SSO IdP.)

### E. Simple check

After 04 finishes:

1. Get the URL the script prints for Keycloak (e.g. `https://auth-osmo.89-169-122-246.nip.io`) or the OSMO UI URL.
2. Open it in a browser. You should be redirected to **Nebius SSO** (`https://auth.nebius.com`), log in, then land back on Keycloak/OSMO.

If that works, SSO is good. No way to “check SSO only” without running from 001-iac through 04; the cluster and Keycloak must exist first.

