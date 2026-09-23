#####################################################################
# NOTE: This is a module and should not be run manually or standalone
#####################################################################

Creates an External Secrets `SecretStore` or `ClusterSecretStore` for Nebius
MysteryBox and an `ExternalSecret` that syncs one MysteryBox secret into a
Kubernetes Secret.

Use this module when Terraform-managed workloads need a Kubernetes Secret
reference but should not read the underlying secret value into Terraform
variables or state.

What this module manages:

- An optional namespace for the ExternalSecret target Secret
- An optional `SecretStore` or `ClusterSecretStore` for the Nebius MysteryBox
  provider
- One `ExternalSecret` that extracts all key/value pairs from one MysteryBox
  secret into one Kubernetes Secret

What this module does not manage:

- Nebius service account creation
- Nebius MysteryBox secret creation
- External Secrets Operator installation
- Any consumer-specific wiring beyond exposing the resulting Secret name
- The bootstrap Kubernetes Secret that holds the Nebius Subject Credentials JSON
  used by the External Secrets provider

Creation boundary:

- Created outside Terraform:
  - The Nebius MysteryBox secret identified by `mysterybox_secret_id`
  - The Nebius service account used by External Secrets
  - The permissions that allow that service account to read MysteryBox payloads
  - The Subject Credentials JSON for that service account
  - The bootstrap Kubernetes Secret that stores that JSON
- Created by this module in Terraform:
  - The target namespace when `create_namespace = true`
  - The `SecretStore` or `ClusterSecretStore` when `create_secret_store = true`
  - The `ExternalSecret`
  - The synced target Kubernetes Secret

Inputs:

- `namespace`
- `create_namespace`
- `secret_store_kind`
- `secret_store_name`
- `create_secret_store`
- `api_domain`
- `service_account_credentials_secret_name`
- `service_account_credentials_secret_key`
- `ca_provider_secret_name`
- `ca_provider_secret_key`
- `external_secret_name`
- `target_secret_name`
- `mysterybox_secret_id`
- `mysterybox_secret_version`
- `refresh_interval`
- `creation_policy`
- `deletion_policy`

Outputs:

- `namespace`
- `secret_store_name`
- `secret_store_kind`
- `external_secret_name`
- `secret_name`
- `mysterybox_secret_id`

Example usage:

```hcl
module "app_secret" {
  source = "../../modules/external-secret-mysterybox"

  namespace                               = "my-app"
  create_namespace                        = true
  secret_store_name                       = "nebius-mysterybox"
  service_account_credentials_secret_name = "nebius-mysterybox-sa-credentials"
  service_account_credentials_secret_key  = "subject-credentials.json"
  target_secret_name                      = "my-app-secret"
  mysterybox_secret_id                    = var.mysterybox_secret_id

  providers = {
    kubernetes = kubernetes
    kubectl    = kubectl
  }
}
```

- If `create_namespace = false`, the target namespace must already exist.

Recommended secret flow:

1. Keep the real secret value in Nebius MysteryBox.
2. Use External Secrets to sync that value into Kubernetes.
3. Pass only the resulting Kubernetes Secret name to downstream modules that
   consume Kubernetes Secrets.

This is better than reading the secret value into Terraform variables because it
keeps the secret material out of Terraform plan output, provider configuration,
and most state paths.

Only roots that actually choose this MysteryBox + External Secrets composition
need a `mysterybox_secret_id` variable. Consumers that use inline credentials
or an already-existing Kubernetes Secret do not need this module at all.

Version behavior:

- If `mysterybox_secret_version` is unset, External Secrets follows the
  MysteryBox primary version and updates the Kubernetes Secret on the next
  refresh after the primary version changes.
- If `mysterybox_secret_version` is set, the sync is pinned to that exact
  MysteryBox version until Terraform is updated to point at a different one.

Expected MysteryBox and Kubernetes Secret structure:

- Store the source secret in MysteryBox as a structured secret whose keys
  already match the contract expected by the consuming chart, operator, or
  application.
- This module uses `dataFrom.extract`, so every key/value pair from the
  MysteryBox secret is copied into the target Kubernetes Secret.
- That means the target Kubernetes Secret schema is determined by the source
  MysteryBox secret schema and by the expectations of the downstream consumer.

Consumer-specific guidance:

- Keep application- or chart-specific key naming requirements in the
  downstream consumer documentation, not here.
- For example, if a consumer expects keys such as `username` / `password` or
  `client_id` / `client_secret`, shape the MysteryBox secret accordingly and
  document that requirement with that consumer module.

Authentication notes:

- The Nebius MysteryBox External Secrets provider currently supports service
  account credentials authentication through
  `serviceAccountCredsSecretRef`, and the API spec also exposes
  `tokenSecretRef`.
- The referenced Kubernetes Secret must hold the Nebius Subject Credentials JSON
  document under the key you pass in
  `service_account_credentials_secret_key`.
- Treat that referenced Kubernetes Secret as bootstrap infrastructure and create
  it out of band. If Terraform creates it, the credential JSON lands in
  Terraform state.

Bootstrap sequence:

Out of band before Terraform:

1. Create a Nebius service account for External Secrets.
2. Grant that service account permission to read MysteryBox payloads for the
   secrets you want to sync.
3. Generate Nebius Subject Credentials JSON for that service account.
4. Create the bootstrap Kubernetes Secret that External Secrets will use for
   authentication.

Created by Terraform after those steps:

5. Run Terraform for `external-secret-mysterybox` and any downstream consumer
   modules.

Example bootstrap Secret creation:

```bash
kubectl create secret generic nebius-mysterybox-sa-credentials \
  -n my-app \
  --from-file=subject-credentials.json=/path/to/subject-credentials.json
```

With that Secret in place, set:

```hcl
service_account_credentials_secret_name = "nebius-mysterybox-sa-credentials"
service_account_credentials_secret_key  = "subject-credentials.json"
```

Process timing:

- The bootstrap Secret must exist before External Secrets can successfully
  reconcile the `SecretStore` and `ExternalSecret`.
- In practice, that means the target namespace must exist first, then the
  bootstrap Secret must be created, and only then can the secret-sync modules
  complete successfully.
- If this module is also creating the target namespace, apply once to create
  the namespace, create the bootstrap Secret out of band, and then apply again
  so the `SecretStore` and `ExternalSecret` can reconcile successfully.

Current limitation:

- This bootstrap step exists because the Nebius MysteryBox provider in External
  Secrets
  (<https://external-secrets.io/main/provider/nebius-mysterybox/>) currently
  documents only secret-backed auth methods for Nebius.
- If the provider later adds alternatives such as workload identity, pod
  identity, metadata-based auth, or direct Kubernetes service-account
  references, this bootstrap Secret requirement could go away.

Design notes:

- `secret_store_kind` defaults to `SecretStore` because namespace-scoped stores
  are usually easier to reason about and safer for tenant isolation.
- Set `create_secret_store = false` when your cluster already has a shared
  `SecretStore` or `ClusterSecretStore` and you only need a new `ExternalSecret`
  plus target Secret.
- This module intentionally outputs only secret references and IDs, never the
  secret value itself.

Best practices:

- Prefer passing `module.<name>.secret_name` into downstream modules instead of
  copying or re-exporting the underlying secret value.
- Keep the External Secrets Operator lifecycle separate from the consuming app
  modules.
- Keep the bootstrap Nebius Subject Credentials JSON out of Terraform and create
  the corresponding Kubernetes Secret with `kubectl` or another out-of-band
  secret delivery path.
- Use MysteryBox version pinning through `mysterybox_secret_version` when you
  need a controlled rollout instead of following the primary version.

References:

- External Secrets Nebius MysteryBox provider:
  <https://external-secrets.io/main/provider/nebius-mysterybox/>
- External Secrets API specification:
  <https://external-secrets.io/main/api/spec/>
