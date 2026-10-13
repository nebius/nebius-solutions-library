# charts/platform

The platform's own manifests for one cluster of the fleet, one component per template directory, rendered by
the platform stage (`stack/platform/manifests.tf`, `data.helm_template`) with the per-cluster values
`stack/config/locals.tf` computes (`platform_values`: hostnames, images, roles, feature flags) and applied
server-side, object by object, in the component's wave next to the upstream Helm releases
(`clusters/common/apps/*.yaml`, `kind: manifests`). It is never installed as a Helm release: Helm would want
its ownership labels on every object, and the templates add no labels of their own.

| Component (template directory) | Wave | Placement | Objects |
|---|---|---|---|
| `node-config` | 0 | all | DaemonSet `spegel/containerd-registry-config`: the containerd registry drop-in and the memlock limit on every node (docs/IMAGES.md) |
| `gateway` | 1 | all | GatewayClass, the external and internal EnvoyProxy (two replicas, PDB, the static address's allocation id) and Gateways, the ACME ClusterIssuers (`edge.acme.email`; none in internal mode), the `models` certificate placeholder the API rewrites, the rate-limit Redis and policy |
| `scheduling` | 1 | all | the `models` namespace and its `endpoints` LocalQueue, PriorityClasses, Kueue WorkloadPriorityClasses |
| `observability` | 2 | all | the alert sink, the cost-export CronJob (the backups bucket; suspended by `observability.cost_export = false`), the Loki datasource, scrape configs; dashboards and alert rules under `files/observability` (Grafana and Prometheus template syntax, emitted untouched) |
| `ops` | 2 | all | namespace `ops`, the spot-node recovery CronJob (the cluster's project and id; suspended on the control cluster), the key-rotation CronJob (rendered suspended under the `-unused` name: keys are Terraform resources), the tenant isolation policy |
| `dispatcher` | 2 | control | the cost-aware MultiKueue dispatcher and the spot price feed (`services/dispatcher`) |
| `knative-serving` | 2 | worker | the KnativeServing CR with this cluster's gateway domain |
| `api` | 3 | all | the customer API (`services/api`): identity and RBAC, the Deployment with its environment and image, the Service, the route on the cluster's `api.` hostname, the per-key rate limit |
| `edge` | 3 | worker | the ReferenceGrant the endpoint SecurityPolicies need |
| `grafana-route` | 3 | all | the Grafana route (external or internal gateway, `edge.grafana_public`) |
| `api-agent`, `fleet-access`, `tenant-clusterrole` | before the waves | worker (`tenant-clusterrole` also control) | the identities the control cluster holds tokens of (the fleet API's `api-agent`, Kueue's `multikueue`) and the ClusterRole bound in every tenant namespace |

`values.yaml` documents every value. `tools/check.sh` renders the chart for every cluster of
`terraform.tfvars.example` and compares the objects with `stack/platform/tests/golden/<cluster>.json`
(`stack/platform/tests/render.py`): names, namespaces, labels, every field and the component must be equal, so that
an apply changes nothing on a live cluster. After a deliberate manifest change, `UPDATE_GOLDEN=1 tools/check.sh
render` rewrites the golden set and the review sees the change in its diff.

Notes carried over from the manifest directories this chart replaced:

- Observability: Alloy, DCGM, Loki and OpenCost are upstream releases (`clusters/common/apps`); this chart owns the
  scrape configs (Knative, queue-proxy, KServe, Kueue, cert-manager, Envoy proxies; the managed PostgreSQL has no
  in-cluster exporter), the recording rules and alerts (`serverless2:*`, receiver `null` of the stack's
  Alertmanager), the Loki datasource and the dashboards (generated, do not edit by hand). `docs/OBSERVABILITY.md`.
- Ops: the ops image (`services/ops`: Nebius CLI, crane, jq, curl) runs the CronJobs with the Secret `ops/nebius-sa`
  the platform stage writes (one `<region>.json` per project). Runbook: `docs/OPERATIONS.md`. The seed-weights Job
  template is `tools/seed-weights-job.yaml` (applied by hand with `envsubst`, never by the stage).
- Scheduling: run classes are admitted through Kueue in namespaces labelled `serverless2.nebius/kueue: managed`
  (every tenant namespace, `charts/tenant`, and `models` for endpoints with a queue name); the ResourceFlavors and
  ClusterQueues come from `charts/fleet`. Never name a LocalQueue in `models` `default` (Kueue's
  LocalQueueDefaulting would label every Deployment there; `docs/ARCHITECTURE.md`).
