# straggler-vmsingle (P26.5 MVP)

A Helm chart that deploys a **dedicated, single-tenant VictoriaMetrics
VMSingle instance** for the GPU straggler-detection pipeline. This is an
MVP for review, not a final production artifact -- see "Open items for
review" below for what's expected to change after this lands.

## Why this exists (don't skip this if you're reviewing dedup/sizing)

The straggler-detection pipeline's `node_aggregator.py` pushes real
aggregate statistics (per-rank mean/CV of collective exec time) on a
~2-6 second cadence per series. This platform's existing shared
VictoriaMetrics instance (`victoria-metrics-k8s-stack`, release `metrics`,
namespace `monitoring-system`) runs `dedup.minScrapeInterval: 30s` --
confirmed directly from that release's own real Helm values
(`helm/soperator-fluxcd/values.yaml`,
`observability.vmStack.values.vmsingle.spec.extraArgs["dedup.minScrapeInterval"]`).

That 30s window was measured, live, to be incompatible:
`P19c_dedup_storage/stage1_dedup_findings.md` pushed 1024 real aggregate
points into two fresh instances (one 0s, one 30s dedup) and found the 30s
instance retained only 160 (**15.6% survival, 84.4% loss**). Worse, CV's
3-window persistence check spans 18 seconds -- entirely inside one 30s
dedup bucket -- so a real, transient fault's confirming window can be
silently discarded by dedup rather than by the persistence logic actually
deciding it wasn't sustained.

Two alternatives to a dedicated instance were considered and rejected:
- **Pre-aggregation harder** (reduce emission rate further): already
  measured as insufficient on its own -- the 84.4% loss above is measured
  *after* aggregate-at-source cadence, not before it.
- **Ask to change the shared instance's dedup setting**: the shared
  instance's 30s dedup is a platform-wide setting serving multiple teams'
  dashboards; changing it for one pipeline's benefit is a policy decision
  outside this pipeline's scope, not a configuration this deployment
  controls.

So: a dedicated instance, with its own `dedup.minScrapeInterval` set low
enough for this pipeline's real cadence. **Live-confirmed below** (not
just re-stated from the earlier report) using the same push-3-values
methodology, against the exact binary/flags this chart configures.

## What this chart deploys

A single `VMSingle` custom resource (`operator.victoriametrics.com/v1beta1`)
-- the **same resource kind the platform's own shared instance already
uses** (confirmed: `helm/soperator-fluxcd/templates/vm-stack.yaml` renders
its `vmsingle:` values block into this exact CR kind, via the official
`victoria-metrics-k8s-stack` chart). This chart does **not** install its
own VictoriaMetrics operator or CRDs -- both are already installed
cluster-wide as a shared, singleton control-plane component (see that same
template's `victoria-metrics-operator-crds` dependency). The existing
operator will reconcile this CR into a real Deployment, PersistentVolumeClaim,
and Service, the same way it already does for the shared instance.

This is deliberately a **thin wrapper**, not a vendored copy of the full
`victoria-metrics-k8s-stack` chart -- that chart also bundles Grafana,
vmagent, kube-state-metrics, and the operator itself, none of which this
dedicated instance needs a second copy of.

## Key values

| Value | Default | Why |
|---|---|---|
| `vmsingle.dedupMinScrapeInterval` | `"0s"` | The entire reason this chart exists -- see above. Matches the standing `~/P20_metrics/` reference instance's own real, already-running flag. Kept as a real, overridable value (not hardcoded) specifically because Evgeny's still-open "0 vm(single\|clusters)" question might mean instance *count*, not dedup -- if so, this value doesn't block that answer. |
| `vmsingle.retentionPeriod` | `"90d"` | Matches the reference instance's real setting exactly. |
| `vmsingle.storage.size` | `30Gi` | See "Resource sizing" below. |
| `vmsingle.resources.requests/limits` | `500m`/`1Gi` request, `2Gi` memory limit, no CPU limit | See "Resource sizing" below. No CPU limit matches the shared instance's own real spec (VM's background merge/compaction work is latency-sensitive to CPU throttling). |
| `vmsingle.port` | `8429` | Matches the shared instance's own port. |
| `vmsingle.livenessProbe` / `readinessProbe` | `{}` (unset) | Deliberately left to the operator's own defaults -- see "Restart/durability" below for why this is not a gap. |
| `fullnameOverride`, `commonLabels` | `""`, `{}` | The only things that need to change for a **second instance** -- see below. |

### Resource sizing -- real basis, not a guess

The standing `~/P20_metrics/` reference instance has been running
continuously for **9.32 real days**, serving *every* real workload shape
this project has tested against it this session (DDP, TP2, TP4, FSDP, MoE,
ResNet, ViT, TP-inference at multiple sizes) -- not a synthetic estimate.
Its own real counters, queried directly:

- `vm_rows_inserted_total{type="prometheus"}` = **2,014,662,130** rows
  over 805,705 seconds uptime = **~2,500 rows/sec average**.
- On-disk size: **1.2GB** for those 2.01B rows = **~0.6 bytes/row**
  compressed (VictoriaMetrics's real, measured compression ratio on this
  exact data shape, not a published/generic number).
- Real peak burst (any single node, any 1-minute window, over the full
  9-day history): `deriv(agg_records_seen_total[1m])` peaked at
  **~170,000 raw NCCL-event/sec** -- this is the *upstream* signal volume
  the aggregator windows down before pushing; actual VM ingestion stays
  far below this thanks to that windowing.

At the measured 2,500 rows/sec average x 90 days x 0.6 bytes/row =
**~12GB** projected. `storage.size` defaults to **30Gi** -- real margin
above that average (also matches the shared platform instance's own real
30Gi setting) because this average reflects *intermittent test-session*
load, not continuous production customer traffic, which hasn't been
measured yet. Resource requests (500m CPU / 1Gi memory) are set with
similar margin above what the reference instance has needed in practice
(it has run under 1 core / a few hundred MB RSS this entire 9-day period).

**Flagged for review**: this sizing has real margin built in precisely
*because* the true sustained multi-job, multi-customer production rate is
still unmeasured -- revisit once real usage data exists, per this MVP's
whole premise.

### A second instance

Per Evgeny's "likely a total of two" note, nothing in this chart is
hardcoded to prevent a second, independent install. Confirmed live
(`helm template`, not just asserted):

```
helm install straggler-vmsingle-b ./straggler-vmsingle \
  --namespace straggler-detection \
  --set fullnameOverride=straggler-vmsingle-b \
  --set vmsingle.storage.size=30Gi
```

produces a distinctly-named `VMSingle` CR (`straggler-vmsingle-b`) with its
own storage, no chart changes required -- verified by rendering both the
default and this override together and diffing the output.

## Install

```
helm install straggler-vmsingle ./straggler-vmsingle --namespace straggler-detection --create-namespace
kubectl -n straggler-detection get vmsingle straggler-vmsingle-straggler-vmsingle
kubectl -n straggler-detection get pods -l app.kubernetes.io/instance=straggler-vmsingle
```

Once the pod is `Ready`, the in-cluster URL for `node_aggregator.py` /
`alert_engine.py` is:

```
http://vmsingle-straggler-vmsingle-straggler-vmsingle.straggler-detection.svc.cluster.local:8429
```

(the operator's real naming convention, `vmsingle-<CR name>` -- confirmed
directly from the shared instance's own Service name,
`vmsingle-metrics-victoria-metrics-k8s-stack`, not guessed. Run
`kubectl -n straggler-detection get svc` once after first install to
double-check the exact rendered name for your release name.)

Point the aggregator at it exactly like the standing local instance:

```
python3 node_aggregator_ref.py <dump_dir> <hostname> \
  http://vmsingle-<release>-<chart>.straggler-detection.svc.cluster.local:8429/api/v1/import/prometheus \
  --duration <secs> --slurm-job-id <id>
```

## Validation performed for this MVP

**Could not be done live, and why**: this session's environment has zero
Kubernetes RBAC beyond self-review APIs (confirmed directly via a
`SelfSubjectRulesReview` against the real cluster API server -- every
namespace, including this session's own `soperator`, returns 403 on every
real resource verb). This is the same restriction an earlier session in
this project already hit and documented (`P20d_dashboard_metrics/final_report.md`:
"still 403 on configmaps/pods in monitoring-system"), now confirmed to
extend to this session's own namespace as well, with zero exceptions
found. **The chart itself was never installed on the real cluster** as a
result -- Steps 3's "pod comes up, Kubernetes auto-restarts it" claims
below are *not* live-cluster-confirmed and should not be read as such.

**What was validated instead, honestly substituted and clearly labeled**:
run the exact same `victoria-metrics-prod` binary this chart's VMSingle CR
configures the operator to run, with the exact same flags this chart
renders (`-dedup.minScrapeInterval=0s`, etc.), locally:

1. **`helm lint` + `helm template`**: chart renders correctly, and every
   field used (`extraArgs`, `retentionPeriod`, `storage.{accessModes,
   resources.requests.storage, storageClassName}`, `resources`, `port`,
   `nodeSelector`, `affinity`, `tolerations`, `podMetadata`,
   `livenessProbe`, `readinessProbe`) was checked directly against the
   real `VMSingle` CRD's OpenAPI schema (pulled from
   `victoria-metrics-operator-crds` via the official chart repo) -- not
   assumed from memory.

2. **Dedup, real A/B**: pushed 3 distinct values for the same series,
   1 second apart, into a local instance running `-dedup.minScrapeInterval=0s`
   (this chart's default) -- all 3 survived (`/api/v1/export` confirmed:
   values `[100, 101, 102]` at their 3 distinct timestamps). The identical
   push against a second local instance running `-dedup.minScrapeInterval=30s`
   (the shared platform instance's real setting) retained only 1 of 3 --
   reproducing the exact real incompatibility this chart's default exists
   to avoid.

3. **Real `node_aggregator_ref.py` data, end to end**: pointed a real
   aggregator process at the 0s-dedup local instance, replaying an
   already-recorded, real ResNet dump (`dump_resnetp26_w0`, from an earlier
   real GPU run in this project -- no new GPU work needed for this chart-
   authoring task). Confirmed via `/api/v1/export` (not just the label-listing
   endpoint, which lagged behind on a freshly-written instance): real
   `agg_samples_seen`, `agg_mean_exec_time_us`, `agg_cv_exec_time`,
   `agg_member_gpu_slot_index`, `agg_mean_windows_total`,
   `agg_cv_windows_total`, and more, all present under the correct
   `slurm_job_id`/`hostname`/`cluster` labels. 59,949 real records
   processed, 72 mean windows, 57 CV windows, 6 successful pushes, 0
   failures.

4. **Durability across a restart**: killed the local VM process (`kill -9`)
   and restarted it pointed at the same on-disk storage path. All 12
   series from the aggregator test, and the dedup test's 3 original
   values, were still present afterward. This proves data survives a
   process restart on persistent storage -- the same property a real
   PersistentVolumeClaim gives a VMSingle pod. **What this does NOT prove**:
   that Kubernetes itself automatically recreates a killed pod -- that's a
   property of the Deployment/StatefulSet controller the operator
   generates from this CR (confirmed real via the CRD schema and the
   shared instance's own currently-running, self-healing deployment), but
   it requires real cluster access to actually exercise, which this
   session doesn't have.

## Open items for review (explicitly not presented as final)

- **Resource/storage sizing** is real-data-based but margin-based, not
  measured against actual sustained production load (see "Resource
  sizing" above) -- revisit once real customer traffic exists.
- **Live cluster install/restart could not be exercised** due to this
  session's RBAC restrictions (see "Validation performed" above) -- this
  needs to actually be installed by someone with real cluster write
  access before it's trusted beyond this MVP review.
- **Probe defaults are inherited from the operator, not customized** --
  intentional for the MVP (matches the shared instance's own real,
  working config), but worth a deliberate look if this pipeline ever
  needs different failure-detection timing than the platform default.
- **The "0 vm(single|clusters)" question from Evgeny is still open.** This
  chart is built so either reading (dedup=0s, or a total instance count of
  0/1/2) is easy to accommodate without a chart rewrite, but the actual
  answer should still be confirmed and reflected in the values used at
  install time.
