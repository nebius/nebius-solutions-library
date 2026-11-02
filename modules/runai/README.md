# Run:ai

Creates a separate control plane or a regional worker on existing Kubernetes.
The control plane uses the published Nebius Marketplace Run:ai package
(wrapper 0.3.1 / Run:ai OSS 3.0.17), private managed PostgreSQL and an optional
Nebius Tunnel public HTTPS URL. Public Terraform resources own PostgreSQL, IAM
and Tunnel separately from the Marketplace Helm release; deleting that release
alone does not delete the database. No Marketplace modification is required.

Workers use NVIDIA's unchanged `runai-cluster` 3.0.17 chart and the documented
registration flow. Keep it aligned with the published control-plane version.
This standard worker step is separate from the Marketplace control-plane package.
The Terraform resource cannot pin the Marketplace product version; check the
published catalog before applying.

## Installation

1. Apply `k8s-training/runai-control-plane.tfvars.example`, supplying protected
   admin/database passwords and an RSA Tunnel key pair. The control plane's
   private network is derived from its cluster subnet. PostgreSQL shape remains
   configurable for the target region.
2. Apply each worker with `public_endpoint = true`, its own Tunnel key pair,
   `cluster_uid = ""` and Dynamo disabled. This creates the real public URL,
   ingress and Prometheus Operator prerequisites without installing a worker.
3. Register that URL through the control plane's native UI/API. Supply the real
   UUID, control-plane URL and fresh bootstrap token, then apply the worker.
4. Verify Connected and a native workload before enabling Dynamo and dependent
   extensions. Enable optional Knative separately after its Ready condition passes.

Use the `runai_url` output for the actual endpoint. Public URLs use Nebius Tunnel
TLS; Run:ai still authenticates and authorizes its API. Tunnel is currently a
Preview service. Each agent receives only the `applicationtunnel.agent` role
scoped to its Tunnel. Agent transport uses port 8443; public HTTPS uses 443.

The root reads credentials from protected files. Ephemeral module inputs and
provider write-only fields keep them out of Terraform state. Tunnel keys and
worker bootstrap tokens are mounted from write-only Kubernetes Secrets, outside
Helm values/history. Initial admin/database credentials use the stock chart's
intended Secret creation. Increment the corresponding revision when rotating
credentials. Keep state and deployment-system access restricted.

For custom worker DNS, set `public_endpoint = false`, supply `domain`, and
prepare trusted TLS at ingress before registration. `values`, `ingress_values`
and `prometheus_values` accept native YAML strings. Worker `values` use the
upstream chart root, without a `runai-cluster` wrapper. Explicit ownership
settings take precedence.

## Prerequisites and GPU ownership

Ingress and Prometheus Operator use the existing Marketplace products
`nebius/nginx-ingress-controller` and `bitnami/kube-prometheus` (wrapper 11.0.0
or later, containing kube-prometheus-stack 92.3.0). Only the Operator is installed;
Run:ai owns its Prometheus server. Set the respective `install_*` flag false to
reuse a compatible existing installation.

Nebius preinstalled-driver images retain their drivers, runtime and device
plugin. The existing Marketplace device-plugin package supplies GFD with native
image affinity; do not add duplicate GFD or GPU Operator installations. Run:ai
owns KAI, Dynamo reuses it, and GPU fractions stay disabled.

For telemetry against Nebius's existing DCGM host engine, the example uses the
stock NVIDIA exporter. Its values are:

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

## Optional Knative inference

No Knative Operator product exists in the checked Marketplace catalog, so this
optional prerequisite uses its unchanged official chart. `serving` accepts raw
`operator_values` and a complete native `KnativeServing.spec` YAML string.
Create its wildcard TLS Secret and route its inference domain to ingress.
The generated management Tunnel URL does not supply wildcard model DNS/TLS.
The kubectl provider does not wait for custom-resource Ready conditions: apply
Serving prerequisites and verify Ready before installing inference-enabled
workers. No custom wait hook or vendor image patch is included.

Customer users, buckets, notebooks and model endpoints remain external through
native APIs. Existing Helm releases require a reviewed ownership transition.

Sources: [NVIDIA worker installation](https://docs.nvidia.com/run-ai/self-hosted/v3.0/documentation/installation-and-configuration/install/manual-helm/install-the-cluster),
[Nebius Tunnels](https://docs.nebius.com/tunnels/connect),
[Knative Operator](https://knative.dev/docs/install/operator/knative-with-operators/).
