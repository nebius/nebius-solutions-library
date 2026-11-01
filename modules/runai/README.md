# Run:ai

Installs a registered regional worker through the canonical **Nebius Marketplace
Run:ai** application. It requires wrapper **0.3.2 or later**, containing the stock
Run:ai OSS **3.0.17** worker. Verify that version is published in your catalog
before applying; the current Nebius resource cannot select a product version.
The module also verifies the stock `runai-operator` Deployment through the
Kubernetes provider, so a release that installs no worker fails verification.

The complete control-plane application includes managed PostgreSQL and a generated
public HTTPS URL. Install it separately through Marketplace Console/API; this
worker module does not create or adopt that deployment graph. Start with the
CPU-only `k8s-training/runai-control-plane.tfvars.example` for its compute.

```hcl
module "runai" {
  source     = "../modules/runai"
  parent_id  = var.parent_id
  cluster_id = var.cluster_id
  config = {
    domain              = "worker.example.com"
    control_plane_url   = "https://runai.example.com"
    cluster_uid         = "<registered native cluster UUID>"
    bootstrap_token_revision = 1
  }
  bootstrap_token = file("/private/runai-bootstrap.token")
}
```

Register the real worker URL using the native UI/API, then supply its UUID and a
fresh bootstrap token. A private worker domain is supported when the intended
callers and control plane can reach its DNS/ingress over trusted TLS. The module
creates `runai-bootstrap` with write-only data and projects the token through the
stock chart's `existingSecret` / `@file` mechanism. The token is not embedded in
Helm values/history or Terraform state. Increment its revision for each rotation.
The caller creates namespace `runai`; `k8s-training` does this automatically.

Prerequisites use existing Marketplace products:

- `nebius/nginx-ingress-controller`, with ingress class `nginx` by default.
- `bitnami/kube-prometheus` wrapper **11.0.0 or later**, using its `stack.*`
  settings to install the Prometheus Operator without a second Prometheus server.

Set `install_ingress` or `install_prometheus_operator` to false to reuse an
existing compatible owner. Run:ai creates its own Prometheus instance. Prepare
worker DNS and `runai-cluster-domain-tls` in `runai` before the worker starts.
`values`, `ingress_values` and `prometheus_values` accept raw native YAML strings;
worker values use the Marketplace root `runai-cluster`. Fixed ownership settings
take precedence. Keep credentials in separately managed Secrets.

One worker owns Run:ai's KAI scheduler; fractions stay disabled. Dynamo must reuse
that scheduler after its API/controllers are available. Do not install a second
KAI or admit the same workload through competing schedulers. Native GPU-image
ownership remains with Nebius; this module does not install drivers.

## Managed-image telemetry and discovery

The supplied example retains separate upstream DCGM-exporter and GFD owners.
In worker values use `runai-cluster.clusterConfig.global.nvidiaDcgmExporter` with
`installedFromGpuOperator: false`, `namespace: dcgm-exporter` and
`serviceMonitor.enabled: true`. Keep GPU Operator's driver/runtime/device-plugin
and DCGM ownership disabled.

DCGM exporter values for Nebius's existing host engine:

```yaml
image:
  repository: nvcr.io/nvidia/k8s/dcgm-exporter
  digest: sha256:b4df763de9558e5b3f1f1d79bc65b772fcf65b8a9c3664ea7173e47153112b4a
arguments: [-r, "127.0.0.1:5555"]
hostNetwork: true
nodeSelector: {nebius.com/driverful: "true"}
tolerations: [{key: nvidia.com/gpu, operator: Exists, effect: NoSchedule}]
serviceMonitor: {enabled: false}
```

GFD uses native-node affinity because Marketplace NFD excludes driverful nodes:

```yaml
devicePlugin: {enabled: false}
gfd: {enabled: true}
nfd: {enabled: false}
affinity:
  nodeAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      nodeSelectorTerms:
      - matchExpressions: [{key: nebius.com/driverful, operator: In, values: ["true"]}]
```

## Optional Knative inference

No standalone Knative Operator product exists in the checked published
Marketplace catalog. This optional prerequisite keeps the official upstream
Operator chart. `serving`
accepts its raw `operator_values` and a complete native `KnativeServing.spec` YAML
string in `spec`; the regional example shows the required domain/features.
There is no local wrapper chart or custom values-merging implementation.

Create the wildcard TLS Secret named by `serving.tls_secret_name` in
`knative-serving`, and route `serving.inference_domain` to the selected ingress.
The caller creates that namespace; `k8s-training` handles it automatically.
Native manifests create KnativeServing/Kourier and its Ingress. The existing
kubectl provider does not wait for custom-resource Ready conditions: install the
Operator/Serving prerequisites first and verify `KnativeServing` Ready before
installing the worker. No custom wait hook or provisioner is used. Endpoint
prediction, authentication and scale-from-zero still require live qualification.
No vendor-image/readiness patch is included for this Marketplace version.

This is a new-installation path; existing Helm releases require a separately
reviewed ownership transition. Customer identities, buckets, notebooks and model
workloads remain owned by the external application through native APIs.

Sources: [Run:ai installation](https://docs.nvidia.com/run-ai/self-hosted/v3.0/documentation/installation-and-configuration/install/manual-helm/install-the-cluster),
[networking](https://docs.nvidia.com/run-ai/self-hosted/v3.0/documentation/installation-and-configuration/system-requirements/networking-setup),
[Knative Operator](https://knative.dev/docs/install/operator/knative-with-operators/).
