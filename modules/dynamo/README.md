# Dynamo

Installs the **Nebius Marketplace NVIDIA Dynamo** application on an existing
cluster through the Nebius provider. Requires the Marketplace Dynamo 1.5.1
wrapper and its native `platform` values; no external Helm chart or Helm
provider is used.

```hcl
module "dynamo" {
  source     = "../modules/dynamo"
  parent_id  = var.parent_id
  cluster_id = nebius_mk8s_v1_cluster.this.id
  config = {
    scheduler_mode = "runai" # standalone installs KAI; external reuses another owner
    grove_mode     = "bundled" # external reuses an installed Grove
    values         = file("dynamo.yaml")
  }
}
```

`runai` and `external` require the existing KAI scheduler to be ready first.
`bundled` installs Grove once. External operators own their configuration and
version compatibility; the Marketplace package configures bundled KAI for Grove's
PodGroup lifecycle.

`config.values` accepts native values YAML nested under `platform`. Ownership
settings take precedence to prevent duplicate controllers. The operator runs
cluster-wide with Kubernetes discovery; etcd, NATS and Snapshot are disabled.
Native CRD installers and hooks remain unchanged. Install workload resources
after the CRDs exist. Keep credentials in separately managed Secrets; provider
settings use write-only fields and a configuration hash to trigger updates.

Run:ai graphs belong in existing project namespaces. Select the native project
queue and node affinity in each component, then verify workload pool, PodGroup
queue and Pod placement.

Run:ai's public URL and cloud resources require its complete Marketplace
application deployment graph. That integration remains a separate prerequisite
for a combined fleet; this Dynamo module does not create those resources.
