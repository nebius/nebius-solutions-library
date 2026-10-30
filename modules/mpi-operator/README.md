# MPI Operator

Installs the stock Kubeflow MPI Operator **0.8.2** multi-document manifest using
HTTP and the existing kubectl provider pattern. The caller supplies the providers.
The vendor Namespace, labels, CRD, RBAC and Deployment are retained; the Namespace
is applied before its namespaced resources. Native server-side apply avoids the
large CRD exceeding Kubernetes' client-side last-applied annotation limit. No
Helm wrapper or vendored YAML is required.

`config.version` pins the upstream release. For an air-gapped deployment, set
`config.manifest_file` to a verified copy of that same vendor manifest with the
approved registry/digest image replacements. File paths are caller-relative.
No image rewriting or application workloads are included. Keep the module's
Namespace out of `platform_namespaces` to retain one owner.

Install workload custom resources after the controller and CRD are ready. Do not
add a broad caller-level `depends_on` that defers HTTP reads until apply: manifest
keys must be available during planning.

Source: [upstream 0.8.2 manifest](https://github.com/kubeflow/mpi-operator/blob/v0.8.2/deploy/v2beta1/mpi-operator.yaml).
