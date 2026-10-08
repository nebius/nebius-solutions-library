# scheduling: priorities and Kueue queues (lane A)

Applied by the Argo CD Application `scheduling` (`clusters/common/apps/scheduling.yaml`):
this shared base (priorities) plus the cluster's ResourceFlavor and `default`
ClusterQueue from `clusters/<cluster>/apps/overlays/scheduling/pool.yaml`.

| Object | Purpose |
|---|---|
| PriorityClass `serverless2-interactive` (1000, PreemptLowerPriority) | endpoints; evicts run-class pods on a full pool |
| PriorityClass `serverless2-batch-priority` (500, Never) | priority runs |
| PriorityClass `serverless2-batch` (100, Never) | default for run-class pods (set in the run WorkflowTemplates, not globalDefault) |
| ResourceFlavor per GPU pool | nodeLabels `serverless2.nebius/pool=<pool>` + the GPU taint toleration; Kueue injects both into admitted pods |
| ClusterQueue `default`, cohort `serverless2` | nominal quota = pool max; preemption within queue by priority, reclaim within cohort |
| ClusterQueue `tenant-template` | the per-tenant pattern, see below |
| WorkloadPriorityClass `customer-batch` (1000) / `bulk-backfill` (100) | queue order and in-queue preemption, label `kueue.x-k8s.io/priority-class` |
| LocalQueue `default` in `spikes` (run class), `models` (inert, namespace not labeled) | only run-class namespaces carry `serverless2.nebius/kueue: managed`; endpoints are never quota-gated |

Kueue runs with `manageJobsWithoutQueueName: false`, but LocalQueueDefaulting
(GA in 0.20, cannot be disabled) labels every Job, JobSet and Pod in a
managed namespace with `kueue.x-k8s.io/queue-name: default` because the
LocalQueue is named `default`. So: label ONLY run-class namespaces; every
pod there is admitted through Kueue. Endpoint namespaces (`models`) must
never carry the label. Argo Workflows have no native Kueue
integration; their pods are admitted through the `pod` integration: the
WorkflowTemplate puts the two labels in `templates[].metadata.labels` and
Kueue's pod webhook adds the scheduling gate `kueue.x-k8s.io/admission`.
Gated pods are ignored by the node-group autoscaler, so the pool only grows
for admitted work.

## Per-tenant pattern

Each tenant gets a namespace labeled `serverless2.nebius/kueue: managed` and
`serverless2.nebius/tenant: <id>`, a LocalQueue `default` in it, and a
ClusterQueue `tenant-<id>` copied from `tenant-template`:

- `cohortName: serverless2` so it can borrow unused quota from `default`
  (bounded by `borrowingLimit`, the per-tenant cap on the shared spot pool);
- `nominalQuota: 0` on the spot flavor for pay-as-you-go tenants, or a
  nominal quota on a reserved flavor (`<tenant>-reserved-…` node pool, own
  ResourceFlavor) for committed capacity, listed first so reserved is used
  before spot;
- `default` reclaims lent quota with `reclaimWithinCohort: Any`, so tenant
  borrowing never blocks the shared queue for long.

Tenant onboarding (lane D) creates these three objects; nothing else changes.
