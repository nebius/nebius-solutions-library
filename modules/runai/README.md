# Run:ai

Installs one upstream OSS Run:ai **3.0.20** Helm release. The caller supplies the
Helm provider and controls optional installation with module `count`/`for_each`.

```hcl
config = {
  role              = "worker" # or control-plane
  domain            = "worker.example.com"
  control_plane_url = "https://runai.example.com"
  cluster_uid       = "<previously registered native cluster UUID>"
  values_files      = ["runai-worker.yaml"]
}
bootstrap_token = file("/private/runai-bootstrap.token")
```

Install the control plane once; register each worker through the native UI/API
before installing that worker. Supply a fresh bootstrap token for every install
or upgrade and increment `config.bootstrap_token_revision` (default `1`) whenever
it must be consumed. Write-only Helm values keep the token out of Terraform
state; Helm stores it in its release history. Keep credentials out of
`values_files`, which are ordinary Terraform state inputs.

Example control-plane values with an existing PostgreSQL 16 database; omit both
`postgresql` blocks to retain the bundled database. Prepare the referenced TLS
and database Secrets in `runai-backend`; the database Secret contains the two
password keys below, supplied outside Terraform.

```yaml
# runai-control-plane.yaml
postgresql: {enabled: false}
global:
  ingress: {tlsSecretName: runai-backend-tls}
  postgresql:
    auth:
      host: postgres.example.com
      port: 5432
      database: runai
      username: runai
      existingSecret: runai-postgresql
      secretKeys: {adminPasswordKey: postgres-password, userPasswordKey: password}
      dbSslMode: require
  customCA:
    enabled: false # Enable when a private CA is needed.
    secret: {name: runai-ca-cert, key: runai-ca.pem, optional: false}
```

Example worker values use path-based workload URLs, without a wildcard
certificate. Prepare `runai-cluster-domain-tls` in `runai`, with a
certificate matching the configured worker domain. When enabling either
optional CA block, create its referenced CA Secret in that role's namespace.

```yaml
# runai-worker.yaml
global:
  customCA:
    enabled: false # Enable to trust a privately signed control plane.
    secret: {name: runai-ca-cert, key: runai-ca.pem, optional: false}
clusterConfig:
  cluster-api:
    ingress:
      tlsEnabled: true
      tls-secret: runai-cluster-domain-tls
  global:
    subdomainSupport: false
    nvidiaDcgmExporter:
      installedFromGpuOperator: true
      namespace: gpu-operator
      serviceMonitor: {enabled: true}
```

Run:ai's native ServiceMonitor discovers the GPU Operator exporter's Service
labels, namespace and metrics port. Keep the GPU Operator's own
`dcgmExporter.serviceMonitor.enabled = false`; Run:ai creates its monitor after
the Prometheus Operator prerequisite is ready. The exporter itself must be
enabled. This example assumes GPU Operator owns the exporter. Managed-driver
images retain their existing GPU stack; configure telemetry separately for that
provider image rather than enabling a second DCGM host engine.

For the supplied managed-driver worker profile, set the worker values above to
`installedFromGpuOperator: false` and `namespace: dcgm-exporter`. Its separate
stock exporter connects to Nebius's existing host engine. Keep its namespace
separate from GPU Operator to avoid the charts' fixed-name counters ConfigMap.
Use these `/secure/dcgm-exporter-values.yaml` values:

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

The managed-image GFD chart also needs native-node affinity: Marketplace NFD
excludes these driverful nodes, so its usual PCI labels are absent. Use this
`/secure/gfd-values.yaml` with the existing discovery release:

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

The module first installs HAProxy **1.54.3** and, for workers,
kube-prometheus-stack **88.6.5** with only its Operator enabled. Set
`install_ingress = false` / `install_prometheus_operator = false` to reuse
compatible existing owners. Their native settings are passed through
`ingress_values_files` / `prometheus_values_files` (both default to `[]`).
HAProxy defaults to a LoadBalancer Service; `ingress_class` defaults to `haproxy`
and configures both controller and Run:ai. Set it to the existing class when
reusing an ingress owner.
Run:ai creates its own Prometheus instance; a basic Prometheus server alone is
insufficient.

Prepare supported Kubernetes, GPU Operator and DNS/TLS first. Native
`values_files` configure TLS secret names, persistence/database, ingress class
and existing operators. Create the documented namespace/TLS Secrets before
the release; secrets and database credentials stay outside this module.
The control-plane role uses the vendor chart's dependency defaults. Worker GPU
fractions default off; enable only with qualified drivers/operator settings.
Knative is an optional prerequisite configured below; training operators retain
their separate owners. The chart retains native CRDs, migrations and hooks.

One Run:ai worker release owns its integrated KAI Scheduler. Do not install a
second KAI release or admit the same workloads through Kueue/another scheduler.
For multiple regions, keep one control plane and distinct worker cluster IDs,
ingress, DNS/TLS and storage. Control-plane-only and worker-only installations
use the same module with the appropriate `role`.

## Native serving prerequisites

Existing configurations leave serving disabled. A worker can additionally own
the official Knative Operator **1.23.0**, Knative Serving **1.23.0**, Kourier and
the wildcard TLS ingress:

```hcl
config = {
  role              = "worker"
  domain            = "worker.example.com"
  control_plane_url = "https://runai.example.com"
  cluster_uid       = "<registered native cluster UUID>"
  serving = {
    inference_domain = "*.inference.example.com"
    tls_secret_name  = "runai-cluster-inference-tls"
    values_files     = ["knative-serving-values.yaml"]
  }
}
```

Prepare that TLS Secret in `knative-serving`, with a certificate covering the
wildcard, before installation. DNS points the wildcard at the existing HAProxy
LoadBalancer. Use `controller.service.annotations` in `ingress_values_files`
for native annotations such as `nebius.com/load-balancer-allocation-id`.
The caller owns the allocation; this module does not allocate or transfer it.

The bundled data-only Helm chart renders a native `KnativeServing` and Ingress.
Knative owns the controllers, Kourier and reconciliation. Its defaults include
HA replicas, scale-to-zero, Run:ai's required native feature flags and
multi-container probing. Serving enables the native Run:ai
`clusterConfig.pod-grouper.args.gangScheduleKnative = false` default; a worker
values file can override it explicitly. Authenticated cold starts also require
the control-plane readiness fix described below; serving prerequisites alone
do not correct the Run:ai 3.0.20 composer race.

`serving.values_files` merges partial native `spec` overrides with these defaults:

```yaml
# knative-serving-values.yaml; optional public-registry overrides.
spec:
  high-availability: {replicas: 2}
  config:
    autoscaler: {scale-to-zero-grace-period: "30s"}
```

For air-gapped installations, set native `spec.registry.override` and
`spec.config.deployment.queue-sidecar-image` to the verified mirrored references.
Knative Operator owns the bundled HPA resources; configure their images through
native `spec.registry.override` rather than declaring duplicate resources.
MPI and other training operators retain their separate platform-manifest owner. For
an Operator-owned extension instead, native `spec.additionalManifests` accepts
manifest URLs reachable by the Operator; keep their versions aligned with
`serving.version` and avoid installing the same resources through another owner.
Set `serving.install_operator = false` to reuse an existing compatible Operator.
Its chart values, including mirrored Operator/webhook images, are passed through
`serving.operator_values_files`.

Optional `chart_path` and `serving.operator_chart_path` select verified local
upstream chart archives instead of their repositories. No runtime credentials
belong in chart values files. Check the native Serving/Kourier/Run:ai conditions
and actual endpoint behavior after applying; Helm completion alone does not
prove custom-resource readiness. Remove native workloads before uninstalling
their serving prerequisite owner.

Sources: [manual Helm preparation](https://docs.nvidia.com/run-ai/self-hosted/v3.0/documentation/installation-and-configuration/install/manual-helm/preparations),
[3.0.20 support matrix](https://github.com/dsx-ai-factory/run-ai/blob/74280199d8665d14e12c88f4cd13dd6a408300ca/docs/installation-and-configuration/support-matrix.md),
[official Knative Operator installation](https://knative.dev/docs/install/operator/knative-with-operators/),
[native Serving configuration](https://knative.dev/docs/install/operator/configuring-serving-cr/).

`ingress_chart_path` and `prometheus_chart_path` optionally select verified local
copies of the same pinned upstream prerequisite charts. Their default repository
and version selections remain unchanged. The parent Kubernetes solution can
create prerequisite namespaces and file-backed write-only Secrets before this
module runs.

## Authenticated cold-start readiness fix

The included [Apache-2.0 patch](patches/0001-gate-authenticated-inference-readiness.patch)
fixes Run:ai **3.0.20** source commit
`74280199d8665d14e12c88f4cd13dd6a408300ca`. Its composer retains the model health
probe and independently probes OAuth's listener, so Knative waits for both.
Authentication and scale-to-zero remain native; no retry or delay is added.
This is a locally maintained patch, not an NVIDIA release.

Apply it to the pinned upstream source and rebuild the official `workloads`
image. `workloadsHelper` uses the same binary; `workloadsManager` is unchanged.
The upstream build requires its documented build prerequisites and Go 1.27.1:

```sh
git clone https://github.com/dsx-ai-factory/run-ai.git
cd run-ai
git checkout --detach 74280199d8665d14e12c88f4cd13dd6a408300ca
git apply --check /absolute/path/0001-gate-authenticated-inference-readiness.patch
git am /absolute/path/0001-gate-authenticated-inference-readiness.patch
(cd control-plane/workloads-and-policies && go test -mod=readonly ./composer/...)
env -u GITHUB_TOKEN make -C control-plane/workloads -o format-go build-go-amd \
  OS=linux ARCH=amd64 GOPATH_HOST_DIR="$(go env GOPATH)" GOCACHE_HOST_DIR="$(go env GOCACHE)"
docker buildx build --platform linux/amd64 --build-arg TARGETARCH=amd64 \
  --provenance=mode=max --sbom=true --push \
  --tag "$WORKLOADS_IMAGE" control-plane/workloads
```

Set `WORKLOADS_IMAGE` to an authorized registry/repository and unique tag before
building. The upstream Dockerfile retains migrations, generated Swagger assets
and NOTICE. Formatting is skipped during the build to preserve the tested tree. Keep the source commit, patch checksum, base-image digests and resulting
image digest with the build receipt. Rebuild for other control-plane CPU
architectures rather than deploying an amd64 binary there.

Pass a native values file through the existing **control-plane**
`config.values_files`; the worker does not own this composer. Pin the published
image by digest (the vendor chart combines `registry/repository:tag`):

```yaml
# runai-readiness.yaml
workloads:
  image: &patchedWorkloads
    registry: registry.example.com
    repository: runai/workloads
    tag: "3.0.20-oauth-readiness@sha256:<published-image-digest>"
workloadsHelper:
  image: *patchedWorkloads
```

```hcl
config = {
  role         = "control-plane"
  domain       = "runai.example.com"
  version      = "3.0.20"
  values_files = ["runai-control-plane.yaml", "runai-readiness.yaml"]
}
```

Prepare registry pull access through the chart's native image-pull configuration.
Keep Knative `multi-container-probing` enabled. Existing Revisions retain their
original probes: recreate a disposable authenticated inference, inspect both
probes, and test the first authorized request after genuine scale-to-zero.
Also verify unauthorized requests remain denied and model-health failure blocks
readiness. Composer regression tests alone do not qualify live ingress behavior.
Live reference validation returned HTTP 200 on three first authenticated requests
after independent scale-to-zero cycles, without retries. A separate unhealthy
model remained unready while OAuth was available; unauthorized requests returned
HTTP 401.
The patch must be reviewed/rebased before changing the control-plane version;
remove the override after qualifying a vendor release containing the fix.
