# Observability and cost

*For operators: dashboards, logs, alerts, cost reports and what they are built from.*

The platform stage applies the shared observability manifests after the upstream Prometheus Operator chart. GPU hardware panels use the upstream NVIDIA DCGM dashboard through kube-prometheus-stack values. Loki logs are stored in per-cluster Object Storage buckets. Retention, the alert webhook and the daily cost report are `terraform.tfvars` inputs (`observability.prometheus_retention_days`, `loki_retention_days`, `alert_webhook_url`, `cost_export`).

## Stack per cluster

| Piece | Where | Notes |
|---|---|---|
| kube-prometheus-stack 91.9 (Prometheus 3.15, Alertmanager, Grafana 13.2, kube-state-metrics, node-exporter) | `monitoring`, app `kube-prometheus-stack` | Prometheus retention `prometheus_retention_days` (30 d) / 45 GB on a 50 GiB PVC, Alertmanager 2 GiB PVC; nil ServiceMonitor/PodMonitor selectors (anything in the cluster is scraped); rules need label `release: kube-prometheus-stack` |
| Loki 7.3 single-binary (Object Storage) | `monitoring`, app `loki` | no auth, no multi-tenancy; retention `loki_retention_days` (30 d) and ingestion limits |
| OpenCost 2.5.32 + alert sink + cost-export CronJob | `opencost` / `monitoring`, app `observability` (child app `opencost`) | cost view and daily report; see "Cost export" and "Alert delivery" below |
| Grafana Alloy 1.13 (DaemonSet on every node: system, GPU and CPU pools; tools/check.sh asserts it tolerates every worker-pool taint) | `monitoring`, child app `alloy` | log shipper: tails every pod on its node through the kubelet API, pushes to Loki |
| NVIDIA DCGM exporter 4.8.4 (DaemonSet on the GPU pools) | `monitoring`, child apps `dcgm-exporter` and `dcgm-exporter-l40s` | node affinity on every `serverless2.nebius/pool` except `system`, tolerates the GPU taint, no GPU request, `honorLabels` so `pod`/`namespace`/`container` are the workload's. Two releases of the same chart: on Nebius L40S VMs the DCGM profiling module cannot watch the `DCGM_FI_PROF_*` fields and the exporter exits ("The third-party Profiling module returned an unrecoverable error"), so `dcgm-exporter-l40s` (namespace `monitoring-l40s`, because the chart hard-codes its ConfigMap and Role names) selects `nebius.com/gpu-name: L40S` with the default counters minus the profiling fields, and `dcgm-exporter` excludes those nodes. L40S nodes therefore have no `DCGM_FI_PROF_*` series (tensor/graphics-engine activity, DRAM activity, PCIe rates); everything else is identical |
| Scrape config, rules, dashboards, Loki datasource | `monitoring`, app `observability` | `charts/platform/templates/observability`, files under `charts/platform/files/observability` |

Grafana: `https://grafana.<cluster host>` on every cluster (user `admin`; the password is a
sensitive output of the platform stage, `./stack.sh output platform control -json credentials`, or
`kubectl -n monitoring get secret grafana-admin`), or
`kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80` when the route is
internal (`edge.grafana_public = false`). The admin password is pinned to the Secret `grafana-admin`
(the platform stage generates it, referenced by `grafana.admin.existingSecret`); without that the chart
re-randomises it on every Helm render and Grafana (emptyDir DB) restarts
with a new one.

There is no OpenTelemetry collector and no tracing; Knative and Kueue
expose Prometheus endpoints directly.

## What is scraped (`scrape.yaml`)

| Target | Object | Endpoint | Key metrics |
|---|---|---|---|
| Knative activator, autoscaler, controller | PodMonitor `knative-control-plane` | `:9090` | `kn_revision_pods_{count,desired,requested,not_ready_count}`, `kn_revision_concurrency_{stable,target,panic}`, `kn_revision_panic_mode`; activator: `http_server_request_duration_seconds_*` (`kn_service_name`, `http_response_status_code`), `kn_revision_request_concurrency` |
| Knative queue-proxy (every endpoint pod) | PodMonitor `knative-queue-proxy` | `http-usermetric` :9091 | `http_server_request_duration_seconds_*` (labels `inferenceservice`, `revision` from the pod labels), `kn_serving_invocation_duration_seconds_*` (app-only latency), `kn_serving_queue_depth` |
| KServe controller | ServiceMonitor `kserve-controller` | `:8443` via kube-rbac-proxy, SA token | controller-runtime metrics |
| Kueue | ServiceMonitor `kueue` | `:8443` https, SA token | `kueue_pending_workloads{cluster_queue,status}`, `kueue_admitted_active_workloads`, `kueue_{admitted,evicted,preempted,finished}_workloads_total`, `kueue_local_queue_resource_usage`, `kueue_cohort_subtree_quota`, `kueue_admission_wait_time_seconds` |
| cert-manager controller | PodMonitor `cert-manager` | `:9402` | `certmanager_certificate_expiration_timestamp_seconds`, `certmanager_certificate_ready_status` |
| Envoy Gateway proxies | PodMonitor `envoy-gateway-proxies` | `:19001/stats/prometheus` | `envoy_cluster_upstream_rq_{total,xx,time_*}` per HTTPRoute cluster (only the request/connection series are kept) |
| DCGM exporter | chart ServiceMonitor | `:9400` | `DCGM_FI_DEV_{GPU_UTIL,FB_USED,FB_FREE,POWER_USAGE,GPU_TEMP,SM_CLOCK}`, `DCGM_FI_PROF_{PIPE_TENSOR_ACTIVE,GR_ENGINE_ACTIVE,...}` with `hostname`, `gpu`, `modelName`, `namespace`, `pod`, `container` and the allow-listed pod labels `serving_kserve_io_inferenceservice`, `job_name` |
| Alloy | chart ServiceMonitor | `:12345` | `loki_write_*`, `loki_source_kubernetes_*` |

Enabling details worth knowing:

- Knative exports nothing by default (OTel protocol `none`). The
  KnativeServing CR carries `config.observability.metrics-protocol:
  prometheus` and `request-metrics-protocol: prometheus`
  (`charts/platform/templates/knative-serving/knative-serving.yaml`). A change
  of those keys needs a restart of the control-plane pods to open :9090;
  endpoint pods pick it up when Knative rolls their revision deployments
  (older pods show as `down` on :9091 until replaced).
- kube-state-metrics (values in `clusters/common/values/kube-prometheus-stack.yaml`): node labels
  `serverless2.nebius/pool`, `serverless2.nebius/capacity`,
  `nvidia.com/gpu.product` and pod labels `serving.kserve.io/inferenceservice`,
  `job-name` (the operation), `kueue.x-k8s.io/queue-name`,
  `serverless2.nebius/tenant` are allow-listed (`kube_node_labels`,
  `kube_pod_labels`); custom-resource state exports
  `kube_inferenceservice_status_condition{name,namespace,type}` (1 = True).
  GPU requests are `kube_pod_container_resource_requests{resource="nvidia_com_gpu"}`
  (KSM sanitises the resource name).
- Spend is read from the fleet database (Cost dashboard, control cluster): the
  platform's spend per key from `platform.keys` (datasource uid
  `platform-postgres`) and LiteLLM's own accounting of the calls that enter
  through it from `litellm.LiteLLM_SpendLogs` (uid `litellm-postgres`;
  LiteLLM's Prometheus callback is an Enterprise feature, `/metrics` answers
  404 in the OSS build). Each is a Secret `serverless2-datasource-<uid>` in
  `monitoring` labelled `grafana_datasource: "1"` (not in git because it
  carries the password), type `grafana-postgresql-datasource`, `sslmode:
  require`, pointing at the managed PostgreSQL: host, port, user and password
  from Secret `api/database`, database `platform` or `litellm`. Without these
  Secrets the Cost dashboard's spend panels stay empty.

## Recording rules (`rules.yaml`, prefix `serverless2:`)

| Rule | Meaning |
|---|---|
| `serverless2:node_pool:info{node,pool,capacity,gpu_product}` | node to pool map from the allow-listed node labels |
| `serverless2:nodes:count_by_pool`, `serverless2:nodes_ready:count_by_pool` | GPU-pool node counts (the `system` pool is excluded) |
| `serverless2:pod_gpu_requests:running{namespace,pod,node,pool}` | GPUs requested by Running pods, pool attached |
| `serverless2:gpu_requests_running:by_pool_namespace`, `:by_isvc` | the same summed per tenant namespace, InferenceService |
| `serverless2:gpu_pods_pending:count`, `serverless2:gpu_pods_unschedulable:count` | GPU pods Pending (incl. Kueue-gated) / unschedulable (scheduler cannot place) |
| `serverless2:gpu_allocatable:by_pool` | allocatable GPUs per pool |

GPU-hours over a dashboard range are `sum_over_time(rule[$__range:1m]) / 60`
(1-minute resolution, absent = 0).

## Dashboards (ConfigMaps `serverless2-dashboard-*`, tag `serverless2`)

Kept as files under `charts/platform/files/observability` (edit the JSON, commit);
never edit them in Grafana. The platform dashboard carries no GPU
hardware row: that is the upstream DCGM dashboard in the Grafana folder `Upstream`.

| Dashboard | uid | Content |
|---|---|---|
| Serverless 2.0 / Platform | `s2-platform` | nodes per pool, Ready/allocatable/requested GPUs, node table with creation time, Node Ready timeline, pool size changes and node joins (scale events), DCGM utilisation/memory/power/tensor-active/temperature, GPU to pod map, GPUs in use by namespace, pending vs running vs unschedulable GPU pods, firing alerts |
| Serverless 2.0 / Endpoints | `s2-endpoints` | InferenceService Ready table and counts, scale-from-zero / scale-to-zero event counts, replicas per service, desired vs requested vs actual per revision, stable concurrency vs target, activator concurrency and queue depth, panic mode, predictor pod startup time (created to Ready), queue-proxy request rate / responses by code / p50 p95 (full and app-only), Envoy per-route rate / 5xx / p95, activator request rate and p95 (the cold start shows here as one slow request) |
| Serverless 2.0 / Jobs | `s2-jobs` | Kueue pending/admitted/evicted/preempted per ClusterQueue, GPU quota usage per LocalQueue, cohort quota, admission wait p50/p95, ClusterQueue active, gate-removal latency, finished workloads (run Jobs are Kueue workloads; per-operation detail is the API and the Logs dashboard) |
| Serverless 2.0 / Cost | `s2-cost` | paid GPU-hours per pool (node time) and their cost at the spot and on-demand prices, burn rate now, used GPU-hours (pod requests), utilisation used/paid, mean DCGM utilisation of busy GPUs, idle GPU-hours, GPU-hours and cost per namespace / pool / InferenceService, hourly cost series, platform spend per key, LiteLLM spend per hour by model, recent priced calls (control cluster) |
| Serverless 2.0 / Logs | `s2-logs` | Loki: run logs by `job` (= operation id), endpoint logs by InferenceService, volume and error-ish lines per namespace, scheduling/scaling component logs, errors anywhere; variables namespace, job, inferenceservice, free-text regex |

Cost dashboard variables (textboxes, USD per GPU-hour): `price_h100_spot`,
`price_rtx_spot`, `price_h100_ondemand`, `price_rtx_ondemand`; set them to
the fleet's `prices`. The priced panels name the pools `h100-spot-1x` and
`rtx6000-spot-1x` and count node-hours as GPU-hours (exact for one-GPU
presets); a fleet with other pools or presets edits those panels. "Paid" is
node time (what the provider bills), "used" is the GPU requests of Running
pods; the difference is scale-down delay, cold-start gaps and nodes that are
kept by stopped preempted VMs.

## Alerts (`rules.yaml`, group `serverless2.alerts`)

| Alert | Condition | Severity |
|---|---|---|
| GPUPodPendingTooLong | a pod requesting a GPU is `unschedulable` for 5 min (Kueue-gated pods do not count) | warning |
| KueueWorkloadsPending | `kueue_pending_workloads > 0` for 15 min per ClusterQueue | warning |
| NodeNotReady | Ready=false for 5 min | critical |
| InferenceServiceNotReady | `kube_inferenceservice_status_condition{type="Ready"} == 0` for 10 min (scaled-to-zero stays Ready) | warning |
| CertificateExpiringSoon / CertificateNotReady | less than 30 days left (self-signed are issued for 1 year, renewed at 2/3) / Ready=False 15 min | warning |
| GPUExporterMissingOnGPUNode | a Ready GPU-pool node without DCGM series for 10 min | info |
| APIErrorRatioHigh | gateway 5xx ratio on the API route above 2 percent for 5 min (with traffic) | critical |
| MonitoringVolumeFillingUp | a PVC in `monitoring` (Prometheus, Alertmanager, Loki) above 80 percent for 15 min | warning |
| AlertmanagerNotifyFailing | a receiver integration failing for 1 h (`alertmanager_notifications_failed_total`) | info |
| CostExportFailed | the daily `cost-export` Job failed | info |

The kube-prometheus-stack default rules (node, kubelet, API server, PVC
filling up, Watchdog) are active as well. Capacity alerts (Kueue admits
against the ClusterQueue quota while the node pool has a maximum and
endpoints hold GPUs Kueue does not see) are in `rules-capacity.yaml`.

## Alert delivery (`alertmanager.config` in `clusters/common/values/kube-prometheus-stack.yaml`)

One receiver on every cluster:

- **In-cluster sink**: Deployment `monitoring/alert-sink` (a 30-line Python
  HTTP server from `alert-sink.yaml`) receives every notification and logs one
  JSON line per alert group (`kubectl -n monitoring logs deploy/alert-sink`,
  or Loki `{namespace="monitoring", app="alert-sink"}`). This proves the
  Prometheus -> Alertmanager -> receiver path without any external system; the
  `Watchdog` alert is routed to it as a daily heartbeat (`/heartbeat`).

No external receiver is configured by default. `observability.alert_webhook_url`
in `terraform.tfvars` adds one webhook (Slack, Teams, PagerDuty, ...) to the
`platform` receiver next to the sink (`stack/platform/stage.tf`). For a
receiver type a plain webhook does not cover: put the credential into a Secret
in `monitoring` (`kubectl -n monitoring create secret generic
alertmanager-slack --from-literal=webhook=https://hooks.slack.com/...`) on
every cluster, list it under `alertmanager.alertmanagerSpec.secrets` and add
the `*_configs` entry with `api_url_file:
/etc/alertmanager/secrets/<secret>/webhook` to the `platform` receiver in
`clusters/common/values/kube-prometheus-stack.yaml`; Alertmanager reads the
file at send time, and the `AlertmanagerNotifyFailing` info alert reports a
receiver that fails for an hour.

Routing: `group_by [alertname, namespace]`, `group_wait 30s`, critical repeats
every 4 h, warning every 24 h, info only to the sink, `InfoInhibitor` to `null`.
Inhibitions: critical silences warning/info of the same alert and namespace;
`NodeNotReady` silences the scheduling, Kueue and endpoint alerts (a lost spot
node explains them). The fleet database (Nebius Managed PostgreSQL) has no
in-cluster alert: LiteLLM's readiness (`/health/readiness`, `db: connected`)
and the API's `/healthz` are the signal; the service monitors the host.

Synthetic test (no real alert needed):

```sh
kubectl -n monitoring port-forward svc/kube-prometheus-stack-alertmanager 9093:9093 &
curl -s -XPOST localhost:9093/api/v2/alerts -H 'Content-Type: application/json' -d '[{"labels":{"alertname":"DeliveryTest","severity":"warning","namespace":"monitoring"},"annotations":{"summary":"synthetic delivery test"}}]'
kubectl -n monitoring logs deploy/alert-sink --since=2m     # one JSON line with alertname DeliveryTest within group_wait
```

## Cost export (OpenCost)

The `opencost` release (chart 2.5.32, namespace `opencost`, every cluster;
`clusters/common/apps/opencost.yaml`, values `clusters/common/values/opencost.yaml`)
reads the stack's Prometheus and prices every pod/node with a custom Nebius
price list (`customPricing`: CPU $0.012/vCPU-h, RAM $0.0032/GiB-h, spot
halves; the GPU on-demand and spot price per cluster are set by the platform
stage from `prices` and the cluster's pools, `stack/platform/stage.tf`; spot =
nodes labelled `serverless2.nebius/capacity=spot`).
Grafana dashboards "OpenCost / Overview" (22208) and "OpenCost / Namespace"
(22252) are loaded by id into the folder `Upstream`; the OpenCost UI is
reachable with `kubectl -n opencost port-forward svc/opencost 9090:9090`
(UI) and `9003` (API, e.g.
`/allocation/compute?window=7d&aggregate=namespace`).

Daily report (`observability.cost_export`): CronJob `monitoring/cost-export`
(00:30 UTC) fetches yesterday's allocation by namespace and by node and uploads
`cost-reports/<cluster>/<date>-{namespace,node}.json` to
the fleet's backups bucket (`<fleet>-backups`, in the hub region) with the Secret
`monitoring/cost-export-s3` (an S3 key of the hub ops SA, written by the
platform stage, `stack/platform/secrets.tf`). OpenCost only knows the time
since it was installed, so the first report of a new cluster is empty and
reports are complete from the second day on; the job fails (and
`CostExportFailed` fires) when the OpenCost API is down, run it by hand with
`kubectl -n monitoring create job cost-export-manual --from=cronjob/cost-export`.
Tenant billing stays on the `serverless2:*` GPU-second rules
(`services/api/billing.py`); OpenCost is the provider-cost view (what the
nodes cost, including idle time) and the two should be compared monthly.

## SLO row (Platform dashboard)

API availability (1 - gateway 5xx ratio on the API route), API p95 latency
(gateway upstream time), endpoint cold start p95 (predictor pod created ->
Ready), run queue wait p95 (Kueue admission wait), the API 5xx ratio over
time and node Ready flips (spot preemptions and joins). Targets the row was
built for: availability >= 99.5 percent, cold start p95 <= 120 s with
pre-pulled images, queue wait p95 <= 10 min at the configured quotas.

## Logs (Loki)

Alloy labels every line with `namespace`, `pod`, `container`, `node`, `app`
and, when the pod carries them, `job` (`job-name`, the operation id),
`inferenceservice` (`serving.kserve.io/inferenceservice`), `revision`
(Knative) and `queue` (Kueue). Useful Explore queries:

```
{job="op-1c254b3c19de66fa", container!~"init|wait"}            # one operation, main container
{inferenceservice="llm-example", container!="queue-proxy"}   # one endpoint's predictor
{namespace="kueue-system"} |~ "Preempted|admitted"             # queue decisions
{namespace="knative-serving", app="autoscaler"} |= "scale"     # scale decisions
```

Loki keeps logs in per-cluster Object Storage buckets with the configured
retention (`limits_config.retention_period`, compactor retention on) and
ingestion limits (8 MB/s, 16 MB burst, 3 MB/s per stream, 50k streams, 256 KB
lines) in `clusters/common/values/loki.yaml`. Each cluster keeps its own logs
and is queried from its own Grafana; the control cluster's Grafana does not
see a region's logs. If the working volume fills up before the retention
ends, `MonitoringVolumeFillingUp` fires; lower the retention.

## Gaps and known limitations

- LiteLLM has no Prometheus metrics in the OSS build; spend comes from its
  Postgres (control cluster) via a hand-made datasource Secret.
- The managed PostgreSQL of the solution exposes no exporter to the cluster;
  its host metrics and backups are in the Nebius console.
- Envoy Gateway emits per-cluster stats for the api/operator HTTPRoutes but
  not for the Knative-managed HTTPRoutes; endpoint traffic is on the
  queue-proxy and activator panels instead.
- Kueue ClusterQueue quota/usage metrics (`kueue_cluster_queue_resource_*`)
  need `metrics.enableClusterQueueResources: true` in the Kueue config
  (`clusters/common/values/kueue.yaml`); the LocalQueue and cohort series
  cover the same numbers.
- No per-tenant Grafana access (single admin); no Thanos/long-term storage
  (Prometheus 30 d / 45 GB on a 50 GiB volume, Loki retention in Object Storage).
- Alerts reach only the in-cluster sink unless `alert_webhook_url` is set;
  other receiver types are a values change, see "Alert delivery".
- OpenCost prices GPU nodes as GPU price plus vCPU/RAM price, slightly above
  the Nebius list (which bundles the vCPUs with the GPU).
- GPU-hours by operation/InferenceService rely on the allow-listed pod labels;
  pods without them show up only per namespace.
