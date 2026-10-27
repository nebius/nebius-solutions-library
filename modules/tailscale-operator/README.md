#####################################################################

# NOTE: This is a module and should not be run manually or standalone

#####################################################################

Installs the Tailscale Kubernetes operator into a cluster and, when needed,
applies the documented Cilium compatibility workaround for clusters that run
with kube-proxy replacement.

Call this module once per cluster. After the operator is installed, use
`modules/tailscale-service` to expose individual Kubernetes services to the
tailnet.

What this module manages:

- A dedicated namespace for the operator, by default `tailscale`
- The Tailscale Kubernetes operator Helm release
- Optional cluster-wide Cilium compatibility configuration for clusters that
need `bpf-lb-sock-hostns-only=true`

What this module does not manage:

- Tailnet ACL policy
- Tailscale OAuth client creation
- Any deployment-specific service exposure
- Per-service Kubernetes `Service` resources

Creation boundary:

- Created outside Terraform:
  - The Tailscale OAuth client in the target tailnet
  - Optional precreated Kubernetes Secrets used with `oauth_secret_name`
  - Optional MysteryBox secrets, Nebius service-account credentials, and
    bootstrap Kubernetes Secrets when you choose the External Secrets path
- Created by this module in Terraform:
  - The operator namespace when `create_namespace = true`
  - The Tailscale operator Helm release
  - Optional Cilium compatibility configuration and restart trigger

Inputs:

- `oauth_client_id`
- `oauth_client_secret`
- `oauth_secret_name`
- `namespace`
- `create_namespace`
- `operator_name`
- `operator_version`
- `operator_hostname`
- `default_tags`
- `enable_cilium_bpf_lb_sock_hostns_only`
- `restart_cilium_after_config_change`
- `cilium_namespace`
- `cilium_config_map_name`
- `cilium_daemonset_name`

Outputs:

- `namespace`
- `helm_release_name`
- `cilium_bpf_lb_sock_hostns_only_enabled`

Example usage:

```hcl
module "tailscale_operator" {
  source = "../../modules/tailscale-operator"

  oauth_client_id     = var.tailscale_oauth_client_id
  oauth_client_secret = var.tailscale_oauth_client_secret
  operator_hostname   = "ts-operator-${var.cluster_name}"
  enable_cilium_bpf_lb_sock_hostns_only = true

  providers = {
    kubernetes = kubernetes
    helm       = helm
  }
}
```

Secret handling:

- Do not commit the OAuth client ID or secret in `terraform.tfvars`.
- Inline Terraform variables are still supported, but they should be treated as
the fallback path:

```bash
export TF_VAR_tailscale_oauth_client_id="tskey-client-..."
export TF_VAR_tailscale_oauth_client_secret="tskey-client-secret-..."
```

- The recommended pattern is to sync the OAuth credentials into a Kubernetes
Secret and set `oauth_secret_name` explicitly.
- `oauth_secret_name` must refer to a Secret in the operator namespace, and
that Secret must contain `client_id` and `client_secret` keys.
- If the Secret is named `operator-oauth`, that happens to match the Tailscale
chart's built-in default Secret name, but that is only a convenience. The
primary contract for this module is the explicit `oauth_secret_name` input.
- If you use a different Secret name, the module wires it into the chart
through `oauthSecretVolume`.
- If you use Nebius MysteryBox plus External Secrets to create that Kubernetes
Secret, the MysteryBox secret ID belongs to the root-module composition that
calls `external-secret-mysterybox`; it is not an input to
`tailscale-operator` itself.
- Keep real values in local environment files only when you are using the
inline-variable path. If an example installation needs documentation, use
comments or empty placeholders rather than real values.
- See `Deployment recipes` below for the full precreated-Secret and
MysteryBox-plus-External-Secrets flows.

Deployment recipes:

Choose exactly one of these patterns for supplying the operator's OAuth
credentials.

Recipe 1: Inline Terraform variables

Use this when you want the shortest path and accept that the OAuth values are
being supplied directly to Terraform at apply time.

Prerequisites:

1. Create a Tailscale OAuth client in the target tailnet.
2. Have a working Kubernetes cluster plus Terraform `kubernetes` and `helm`
  providers.

Created outside Terraform:

- The Tailscale OAuth client
- The local environment variables that supply the OAuth values

Created by Terraform:

- The Tailscale operator namespace and Helm release
- Optional Cilium compatibility configuration

Steps:

1. Export the OAuth values locally:

```bash
export TF_VAR_tailscale_oauth_client_id="tskey-client-..."
export TF_VAR_tailscale_oauth_client_secret="tskey-client-secret-..."
```

1. Call `tailscale-operator` with inline credentials:

```hcl
module "tailscale_operator" {
  source = "../../modules/tailscale-operator"

  oauth_client_id     = var.tailscale_oauth_client_id
  oauth_client_secret = var.tailscale_oauth_client_secret
  operator_hostname   = "ts-operator-${var.cluster_name}"

  providers = {
    kubernetes = kubernetes
    helm       = helm
  }
}
```

Recipe 2: Precreated Kubernetes Secret

Use this when another system already creates Kubernetes Secrets for you, or
when you want to keep OAuth values out of Terraform inputs without adopting
External Secrets.

Prerequisites:

1. Create a Tailscale OAuth client in the target tailnet.
2. Have a working Kubernetes cluster plus Terraform `kubernetes` and `helm`
  providers.
3. Create a Kubernetes Secret in the operator namespace containing:
  - `client_id`
  - `client_secret`

Created outside Terraform:

- The Tailscale OAuth client
- The Kubernetes Secret referenced by `oauth_secret_name`

Created by Terraform:

- The Tailscale operator namespace and Helm release
- Optional Cilium compatibility configuration

Example Secret creation:

```bash
kubectl create secret generic operator-oauth \
  -n tailscale \
  --from-literal=client_id='tskey-client-...' \
  --from-literal=client_secret='tskey-client-secret-...'
```

Steps:

1. Create the Kubernetes Secret before running Terraform for this module.
2. Reference it explicitly:

```hcl
module "tailscale_operator" {
  source = "../../modules/tailscale-operator"

  oauth_secret_name = "operator-oauth"
  operator_hostname = "ts-operator-${var.cluster_name}"

  providers = {
    kubernetes = kubernetes
    helm       = helm
  }
}
```

Recipe 3: Nebius MysteryBox plus External Secrets

Use this when you want the OAuth values stored in MysteryBox and synced into
Kubernetes without Terraform ever reading the actual secret payload.

Prerequisites:

1. Create a Tailscale OAuth client in the target tailnet.
2. Create a MysteryBox secret containing:
  - `client_id`
  - `client_secret`
3. Have a working Kubernetes cluster plus Terraform `kubernetes`, `helm`, and
  `kubectl` providers.
4. Create a Nebius service account for External Secrets.
5. Grant that service account permission to read MysteryBox payloads.
6. Generate Nebius Subject Credentials JSON for that service account.
7. Create the bootstrap Kubernetes Secret that External Secrets will use for
  authentication.

Created outside Terraform:

- The Tailscale OAuth client
- The MysteryBox secret containing `client_id` and `client_secret`
- The Nebius service account used by External Secrets
- The service account's Nebius permissions
- The Subject Credentials JSON for that service account
- The bootstrap Kubernetes Secret that stores that JSON

Created by Terraform:

- The External Secrets Operator release
- The `SecretStore` and `ExternalSecret` that sync the MysteryBox secret
- The synced Kubernetes Secret consumed by `tailscale-operator`
- The Tailscale operator namespace and Helm release
- Optional Cilium compatibility configuration

Bootstrap Secret example:

```bash
kubectl create secret generic nebius-mysterybox-sa-credentials \
  -n tailscale \
  --from-file=subject-credentials.json=/path/to/subject-credentials.json
```

Recommended order:

1. Install `external-secrets-operator`.
2. Ensure the operator namespace exists.
   If you want Terraform to create that namespace, apply once to create it,
   then create the bootstrap Secret out of band, and then apply again for the
   secret-sync and operator modules.
3. Create the bootstrap Secret in that namespace.
4. Apply `external-secret-mysterybox` to sync the OAuth secret into Kubernetes.
5. Apply `tailscale-operator` with `oauth_secret_name` pointing at the synced
  Secret.

Example composition:

```hcl
module "external_secrets_operator" {
  source = "../../modules/external-secrets-operator"

  providers = {
    kubernetes = kubernetes
    helm       = helm
  }
}

module "tailscale_oauth_secret" {
  source = "../../modules/external-secret-mysterybox"

  namespace                               = "tailscale"
  create_namespace                        = false
  secret_store_name                       = "nebius-mysterybox"
  service_account_credentials_secret_name = "nebius-mysterybox-sa-credentials"
  service_account_credentials_secret_key  = "subject-credentials.json"
  target_secret_name                      = "tailscale-operator-oauth"
  mysterybox_secret_id                    = var.mysterybox_secret_id

  providers = {
    kubernetes = kubernetes
    kubectl    = kubectl
  }

  depends_on = [module.external_secrets_operator]
}

module "tailscale_operator" {
  source = "../../modules/tailscale-operator"

  oauth_secret_name = module.tailscale_oauth_secret.secret_name
  operator_hostname = "ts-operator-${var.cluster_name}"

  providers = {
    kubernetes = kubernetes
    helm       = helm
  }
}
```

Important limitation:

- This bootstrap Secret exists because the current Nebius MysteryBox provider
in External Secrets
([https://external-secrets.io/main/provider/nebius-mysterybox/](https://external-secrets.io/main/provider/nebius-mysterybox/)) documents
only secret-backed auth methods for Nebius.
- If the provider later adds workload identity, pod identity,
metadata/default-credential-chain auth, or direct Kubernetes service-account
references, this bootstrap step could become unnecessary.

Safe OAuth rotation with External Secrets:

- This path was validated with the Nebius MysteryBox plus External Secrets
  flow by promoting a new MysteryBox version to primary and then creating
  brand-new Tailscale-backed services on both `k8s-training` and `soperator`.
- Fresh service creation worked after the new secret version synced into the
  Kubernetes Secret consumed by `oauth_secret_name`.
- Cleanup of older operator-managed proxy services is a different path. After
  revoking the old Tailscale OAuth credential, deleting an older existing
  proxy-backed Service on `k8s-training` failed cleanup with operator logs
  showing `401 Unauthorized` and `API token invalid` while deleting the
  corresponding device.
- This matches Tailscale's documented trust-credential behavior, where
  revoking a trust credential immediately revokes API access created from it:
  [https://tailscale.com/docs/reference/trust-credentials](https://tailscale.com/docs/reference/trust-credentials)
- The operator's credential usage is documented here:
  [https://tailscale.com/docs/features/kubernetes-operator](https://tailscale.com/docs/features/kubernetes-operator)

Recommended rotation order:

1. Create the new OAuth credential and store it in a new MysteryBox secret
   version.
2. Promote that version to primary.
3. Wait for External Secrets to refresh the Kubernetes Secret referenced by
   `oauth_secret_name`.
4. Create a brand-new Tailscale-backed Service and confirm it boots and serves
   traffic.
5. Delete any older operator-managed Tailscale Services you still want cleaned
   up while the old credential is still valid.
6. Revoke the old OAuth credential only after the fresh-create test succeeds
   and old-service cleanup is complete.

Operational note:

- If you revoke the old credential first, fresh service creation can still
  succeed with the new credential, but cleanup of older proxy-backed Services
  may fail until you intervene manually.
- A cleaner long-term direction is Tailscale operator workload identity
  federation, which Tailscale documents as beta in the Kubernetes operator
  docs, because it avoids long-lived OAuth client secret rotation entirely.

Testing workflow:

1. Create the OAuth client in the target tailnet.
2. If you are using the External Secrets path, create the MysteryBox secret that
  stores the Tailscale OAuth `client_id` and `client_secret`.
3. Choose one credential path:
  - export the OAuth values locally through `.envrc` or equivalent for the
   inline-variable path
  - reference a precreated Kubernetes Secret with `oauth_secret_name`
  - or sync the OAuth values into a Kubernetes Secret with External Secrets and
  then pass `oauth_secret_name`
4. If you use the External Secrets path, first install External Secrets
   Operator, ensure the target namespace exists, create the Nebius Subject
   Credentials bootstrap Secret in that namespace, and then apply the
   secret-sync module.
5. Run `terraform init` and `terraform validate` in the calling root module.
6. Apply the operator module once per cluster.
7. Apply one or more `tailscale-service` modules for the workloads you want to
  expose.
8. Confirm the resulting MagicDNS name appears in the tailnet admin console and
  test connectivity from an authorized device.

Notes:

- This module expects working `kubernetes` and `helm` providers.
- `enable_cilium_bpf_lb_sock_hostns_only` defaults to `true` because Nebius MK8s  
clusters use Cilium in a mode that breaks Tailscale proxy return traffic  
unless `bpf-lb-sock-hostns-only=true` is set. Override it to `false` only if  
you have confirmed your target cluster does not need the workaround.
- Manage Tailscale ACLs separately in the tailnet admin console.
