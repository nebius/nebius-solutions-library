# Early Flux bootstrap with the unmodified main chart

`installations/example` and `installations/pikachu` use this sequence:

1. Create the Kubernetes control plane and configure kubectl.
2. Install Flux and seed `terraform-fluxcd-values` without replacing an existing
   ConfigMap. Install the umbrella chart with Helm, with waiting disabled and a
   post-renderer that retains only namespaces, cert-manager, storage classes,
   Kruise, and their four Helm repositories.
3. Create node groups. Flux controllers start on system nodes and reconcile the
   four foundation HelmReleases.
4. After node creation, check the four releases. For a release that is not Ready
   at its current generation, set `reconcile.fluxcd.io/requestedAt` and
   `reconcile.fluxcd.io/resetAt` with kubectl and wait for readiness.
5. The raw Helm release adopts the seed ConfigMap and writes the full Terraform
   values. Install the Flux bootstrap chart and resume/reconcile the umbrella.
   Flux takes over the existing Helm release and upgrades it with the full values.

## Compatibility with main

The main chart gates Kruise and its repositories on `soperator.enabled` and
renders Slurm even when `slurmCluster.enabled=false`. Bootstrap therefore sets
`soperator.enabled=true` and filters the rendered manifests before Helm installs
anything. The filter validates that all four required HelmReleases, their
repositories, and their dependencies are present. It runs against the exact
chart selected by `slurm_operator_version`, not a modified local checkout.
No `kruise.standalone` flag or chart patch is required.

The umbrella is initially installed as `<flux namespace>-soperator-fluxcd`,
matching the release name Flux derives from `targetNamespace` and the
`soperator-fluxcd` HelmRelease. The release and its child resource ownership stay
unchanged during [Flux takeover](https://fluxcd.io/flux/migration/helm-operator-migration/).

Main does not create `storage-system`. The bootstrap script creates it separately
so an upgrade with the unmodified chart cannot prune it from the namespace
release. The namespace is removed with the Kubernetes cluster.

## Retries and existing installations

A deployed `terraform-fluxcd-values` Helm release marks the full configuration
phase. If it exists, the initial install script leaves the running stack intact.
Otherwise, an existing Flux umbrella is suspended before Helm repairs the
foundation; it is resumed only after the readiness gate and full ConfigMap write.
This also handles the previously broken bootstrap that omitted Kruise and the
Soperator HelmRepository. A failure leaves the umbrella suspended for the next
Terraform retry, keeping the rest of the stack disabled.

The early completion output depends only on the initial Helm install. The Flux
bootstrap depends separately on the full ConfigMap, avoiding a dependency cycle
with node creation. Existing Terraform bootstrap resource addresses are retained.

## Requirements and checks

The Terraform runner needs `helm` (v3 or v4), `kubectl`, `jq`, and Mike Farah `yq` v4.
Helm 3 runs the post-renderer executable directly. Helm 4 uses a temporary local
post-renderer plugin directory; no persistent plugin installation is required.
No Flux CLI is used for reconciliation. Python 3 is needed only for local tests.

Run shell/state tests with:

```sh
python3 -B -m unittest discover -s soperator/modules/fluxcd_bootstrap/tests
```

For chart compatibility tests, export `SOPERATOR_MAIN_CHART` pointing to
`helm/soperator-fluxcd` from an unmodified main checkout/archive. Keep the sibling
`helm/soperator-fluxcd-bootstrap` directory alongside it, then run the same
command. These tests render the real charts, verify the exact allowlist and
release identity, and reject missing repositories or unsupported dependencies.
They do not contact a cluster.
