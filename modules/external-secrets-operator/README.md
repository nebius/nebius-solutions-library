#####################################################################
# NOTE: This is a module and should not be run manually or standalone
#####################################################################

Installs the External Secrets Operator into a Kubernetes cluster with Helm.

Use this module when workloads in the cluster need to consume Kubernetes
Secrets that are synced from an external system such as Nebius MysteryBox.

What this module manages:

- An optional namespace for the External Secrets Operator
- A Helm release for the External Secrets Operator
- External Secrets CRD installation when enabled

What this module does not manage:

- Any `SecretStore`, `ClusterSecretStore`, or `ExternalSecret` resources
- Nebius service account creation
- Nebius MysteryBox secret creation
- Consumer-specific secret wiring

Creation boundary:

- Created outside Terraform:
  - The Nebius service account used by the MysteryBox provider
  - The permissions that allow that service account to read MysteryBox payloads
  - The Subject Credentials JSON for that service account
  - The bootstrap Kubernetes Secret that stores that JSON
- Created by this module in Terraform:
  - The External Secrets namespace when `create_namespace = true`
  - The External Secrets Operator Helm release and CRDs

Inputs:

- `namespace`
- `create_namespace`
- `release_name`
- `repository_url`
- `chart_name`
- `chart_version`
- `install_crds`
- `atomic`
- `wait`
- `timeout_seconds`
- `values`

Outputs:

- `namespace`
- `helm_release_name`
- `chart_version`

Example usage:

```hcl
module "external_secrets_operator" {
  source = "../../modules/external-secrets-operator"

  providers = {
    kubernetes = kubernetes
    helm       = helm
  }
}

module "app_secret" {
  source = "../../modules/external-secret-mysterybox"

  namespace                               = "my-app"
  create_namespace                        = true
  secret_store_name                       = "nebius-mysterybox"
  service_account_credentials_secret_name = "nebius-mysterybox-sa-credentials"
  service_account_credentials_secret_key  = "subject-credentials.json"
  target_secret_name                      = "my-app-secret"
  mysterybox_secret_id                    = var.mysterybox_secret_id

  depends_on = [module.external_secrets_operator]

  providers = {
    kubernetes = kubernetes
    kubectl    = kubectl
  }
}
```

- If the target namespace already exists, you can leave
  `create_namespace = false` in the downstream secret-sync module.


Recommended layering:

1. Install `external-secrets-operator`
2. Sync provider-backed secrets with `external-secret-mysterybox`
3. Pass only the resulting Kubernetes Secret name into consumer modules

Only the roots that opt into this provider-backed secret flow need to carry a
MysteryBox secret ID variable. This module itself stays generic and does not
assume any specific consumer.

Bootstrap note:

- External Secrets still needs one bootstrap credential so it can talk to the
  upstream secret backend.
- For the Nebius MysteryBox provider, that bootstrap credential is a
  Kubernetes Secret containing Nebius Subject Credentials JSON for a service
  account with MysteryBox read access.
- Create that bootstrap Secret out of band instead of through Terraform. If
  Terraform creates it, the service-account credential material ends up in
  Terraform state, which defeats the point of this pattern.
- The current Nebius provider docs for External Secrets
  (<https://external-secrets.io/main/provider/nebius-mysterybox/>) describe
  only secret-backed auth for MysteryBox. That is why this bootstrap Secret
  still exists in the process today.

Recommended order of operations for the Nebius MysteryBox path:

Out of band before Terraform:

1. Create the Nebius service account and grant it MysteryBox read access.
2. Generate the Subject Credentials JSON for that service account.
3. Create the bootstrap Kubernetes Secret containing that JSON in the target
   namespace.

Created by Terraform:

4. Install `external-secrets-operator`.
5. Ensure the target namespace exists. If Terraform will create it through
   `external-secret-mysterybox`, apply once to create the namespace, then
   create the bootstrap Secret out of band, and then apply again.
6. Apply `external-secret-mysterybox` to create the `SecretStore`,
   `ExternalSecret`, and synced target Secret.
7. Apply the downstream consumer that references the synced Kubernetes Secret.

References:

- External Secrets Operator getting started:
  <https://external-secrets.io/latest/introduction/getting-started/>
- External Secrets Nebius MysteryBox provider:
  <https://external-secrets.io/main/provider/nebius-mysterybox/>
