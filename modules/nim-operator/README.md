# NIM Operator

Installs the published NVIDIA `k8s-nim-operator` **3.1.2** chart. The caller owns
the Helm/Kubernetes providers and enables the module with `count`/`for_each`.

```hcl
config = {
  version            = "3.1.2"
  values_files       = ["nim-operator-values.yaml"]
  runai_cache_reader = {} # Optional native read-only Run:ai integration.
}
```

`namespace` defaults to `nim-operator`. Optional `chart_path` selects a verified
local upstream `.tgz` instead of the public repository. Native `values_files`
can select CPU nodes and mirrored/pinned images using `operator.nodeSelector`,
`operator.image.repository` and `operator.image.tag`. Credentials are existing
Secret references; keep secret values out of these ordinary Terraform inputs.

GPU Operator and its NFD/device-plugin ownership must already be established.
This module always disables NIM Operator's bundled older Dynamo dependency;
install Dynamo/Grove separately through their existing owner. The upstream
chart retains its CRDs and native CRD upgrade Job. Its admission webhook remains
at the upstream default (disabled); enabling it requires the documented TLS
prerequisites.

`runai_cache_reader = {}` adds only `get/list/watch` for `apps.nvidia.com/nimcaches`
to Run:ai's existing `runai/cluster-sync` ServiceAccount. Override `namespace`
and `service_account` inside that object for a different native installation.
Run:ai 3.0.20 already reads NIMServices but omits NIMCaches from that role.

The project owner creates NGC registry/API Secrets, owner PVCs and native
NIMCache/NIMService manifests. Install the operator first, then plan/apply these
custom resources after its CRDs are available; a Terraform dependency alone
does not solve custom-resource schema discovery during planning. Preserve owner
cache PVCs independently of temporary serving workloads. Installing the chart
does not download model weights or prove a cache/model ready.

Sources: [official installation](https://docs.nvidia.com/nim-operator/latest/install.html),
[published v3.1.2 chart](https://github.com/NVIDIA/k8s-nim-operator/tree/v3.1.2/deployments/helm/k8s-nim-operator),
[native model-cache workflow](https://docs.nvidia.com/nim-operator/latest/cache-non-llm.html).
