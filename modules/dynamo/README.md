# Dynamo

Installs the existing **Nebius Marketplace NVIDIA Dynamo** application on an
existing cluster through the Nebius provider. The current Marketplace baseline
is Dynamo **1.1.1**, bundled Grove **v0.1.0-alpha.7** and optional KAI **v0.13.4**.
This integration does not upgrade that package or fetch external Helm charts.

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
version compatibility. `config.values` accepts native chart YAML at its original
root paths, such as `dynamo-operator.controllerManager.affinity`.

Ownership settings take precedence to prevent duplicate controllers. The operator
runs cluster-wide with Kubernetes discovery. Both the NATS deployment and operator
connection are disabled, along with bundled etcd; this package has no Snapshot
dependency. Native CRD installation and hooks remain unchanged. Install workloads
after the CRDs exist. Keep credentials in separately managed Secrets; provider
settings use write-only fields and a configuration hash to trigger updates.

This baseline uses the `v1alpha1` Dynamo workload API. `v1beta1` workloads and
Run:ai component-affinity-to-node-pool integration require a separate Marketplace
upgrade and runtime qualification. Verify workload ownership, queues and actual
Pod placement before claiming a working integration.

The separate Run:ai module owns its control plane, database and optional native
public URLs; this Dynamo module does not create them.
