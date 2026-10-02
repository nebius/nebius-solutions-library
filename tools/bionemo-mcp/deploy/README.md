# Deployment

Deploy `nebius-bionemo-mcp` either into the task-owned NIM namespace or into a
dedicated gateway namespace. The chart does not create NIMs, Object Storage,
registry, TLS, ingress-controller, or credential resources. Provision those as
fresh resources first and pass only their non-secret identifiers to Helm.

## Required values

- `image.repository` and immutable `image.digest`
- direct `modules/nims` `nim_catalog` output under `nimCatalog`
- `auth.existingSecret`, containing a random bearer token in `token`
- `objectStorage.bucket`, `endpointUrl`, `region`, and `existingSecret`
- an HTTPS ingress host and TLS Secret for external client access
- ingress-controller selectors matching the task-owned controller
- monitoring namespace/pod selectors if Prometheus scrapes NIM ServiceMonitors

Keep `service.type=ClusterIP`. The chart rejects another value because an L4
LoadBalancer would also expose the unauthenticated internal health route. The
Ingress must route exactly `/mcp`; the example uses `pathType: Exact`.

The default ingress-nginx annotations disable request and response buffering and
set read/send timeouts to 7200 seconds so long model and pipeline calls are not
terminated by the ingress controller. Preserve those defaults when adding
cert-manager annotations. For another ingress implementation, configure its
equivalent streaming and upstream timeout settings explicitly.

## Catalog overlay

```bash
terraform -chdir=<nim-deployment> output -json nim_catalog \
  | jq '{nimCatalog: .}' > catalog-values.json
```

Do not edit model URLs or ports in the overlay. `CatalogEntry` validates service
name, namespace-qualified cluster DNS, HTTP scheme, and port before the server
starts. Enabled but unready models remain visible in `fleet_health` but do not
receive model tools. Pipelines are registered only when all component models are
healthy.

## Network isolation

`networkPolicy.nimIsolation.enabled=true` selects every enabled model's
catalog-exported `pod_selector_labels` and permits inference/metrics port ingress
only from gateway pods and configured monitoring clients. Verify the cluster CNI
enforces NetworkPolicy.

For a dedicated gateway namespace, set the NIM namespace and its labels so the
gateway egress policy and NIM ingress policy are rendered in the correct
namespaces:

```yaml
networkPolicy:
  nimIsolation:
    enabled: true
    namespace: nims
    namespaceLabels:
      kubernetes.io/metadata.name: nims
```

Leave `namespace` and `namespaceLabels` empty only when gateway and NIM pods are
co-located in the Helm release namespace.

Before acceptance, prove that:

1. a pod without an allowed label cannot connect to any NIM ClusterIP or legacy
   NIM LoadBalancer address;
2. the gateway can probe and invoke each enabled NIM;
3. Prometheus can still scrape `/v1/metrics` when its selectors are configured;
4. only `https://<host>/mcp` is externally routed;
5. missing, malformed, and valid bearer headers produce `401`, `401`, and an MCP
   response respectively.

## Rollout and cleanup

Run `helm test` after rollout and record the chart version, image tag/digest,
namespace, ingress address, catalog image versions, and Object Storage artifact
hashes. Delete the Helm release before destroying the dedicated cluster, then
delete and verify deletion of the task-owned bucket, registry, service credentials,
filesystem, GPU node group, cluster, subnet, and network.
