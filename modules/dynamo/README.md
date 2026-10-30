# Dynamo

Installs the upstream **Dynamo 1.5.1** platform chart. Its bundled dependencies
pin **Grove v0.1.0-alpha.13** and **KAI v0.17.0**. The caller supplies the Helm
provider and controls optional installation with module `count`/`for_each`.

```hcl
config = {
  scheduler_mode = "runai" # standalone installs KAI; external reuses another KAI owner
  grove_mode     = "bundled" # external reuses an already installed Grove
  values_files   = ["dynamo.yaml"]
}
```

`runai` and `external` never install KAI; the existing scheduler must be ready
first. Run:ai 3.0.20 already owns KAI v0.18.3. `bundled` installs Grove exactly
once through the platform chart; do not also install a separate Grove release.
Upstream recommends independent Grove/KAI lifecycle for production: select
`external` after preparing those compatible releases. Ownership settings override
`values_files` so an overlay cannot accidentally install another scheduler.
Bundled KAI disables stale PodGroup eviction (`default-staleness-grace-period =
"-1s"`) as the vendor chart requires when Grove controls termination.

Native Kubernetes discovery is used; etcd, NATS and Snapshot remain disabled.
Native CRD installers, certificates and Helm hooks are retained. Kubernetes
1.30 or later is required by this chart; the stricter Run:ai matrix applies to
combined installations. Install workload custom resources in a later phase once
the CRDs exist. Native values files can configure controller placement, image
mirrors, metrics and ingress. They are ordinary Terraform state inputs; keep
credentials in separately managed Secrets.

Run:ai-managed graphs belong in existing project namespaces. Select the native
project pool queue and required node affinity in every component, then verify
the final Run:ai workload pool, PodGroup queue and Pod placement. Controller
readiness alone does not establish workload or RDMA readiness.

Sources: [pinned platform chart](https://github.com/ai-dynamo/dynamo/blob/v1.5.1/deploy/helm/charts/platform/Chart.yaml),
[native values and ownership](https://github.com/ai-dynamo/dynamo/blob/v1.5.1/deploy/helm/charts/platform/values.yaml),
[Run:ai integration](https://github.com/dsx-ai-factory/run-ai/blob/74280199d8665d14e12c88f4cd13dd6a408300ca/runai-installer/installer/internal/orchestrator2/steps/prereq_dynamo.go).

Use `config.chart_path` for a verified local upstream chart archive and
`values_files` for regional image pins. `release_name` defaults to
`dynamo-platform`; set it when reproducing an existing native release name.
