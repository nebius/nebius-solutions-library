# Standalone Serverless UI

React 18, TypeScript, Vite, Gravity UI, and Recharts. The theme follows the Nebius Console
Serverless reference: Inter, a charcoal sidebar, white pages, violet actions, compact
tables, sectioned creation forms, and tabbed resource details. The Nebius wordmark in
`public/nebius-logo.svg` comes from the console's public brand asset at
`https://static.nebius.com/common/illustrations/nebius/logo_com.svg`.

The production application talks to `services/api`. Monitoring is rendered in the UI.
Sample data is included only in the separate local preview build.

## Local visual preview

Start a single local Docker container to review all screens without signing in or running
an API, database, Kubernetes cluster, Prometheus, Loki, or Grafana:

```sh
cd k8s-inference/ui
docker compose -f compose.preview.yaml up --build -d
```

Open **http://localhost:5188**. The header identifies the preview and its sample data.
Endpoints, jobs, charts, logs, attempts, results, definitions, and API keys use data inside
the browser. Resource changes affect only the open tab and reset on reload. No resource
action contacts a cloud service. The container serves static files with nginx and has no
API proxy. Stop it with `docker compose -f compose.preview.yaml down`.

`Dockerfile.preview` builds with `VITE_UI_PREVIEW=true`; the normal Dockerfile keeps the
production API connection. Preview data is excluded from the production bundle.

| Page | Behavior |
| --- | --- |
| Endpoints | Search, status/region filters, create action, regional identities, logs and metrics actions |
| Endpoint detail | Overview with inline monitoring; Metrics, Logs, Scaling, Test request, Settings; saved configuration; confirmed deletion |
| Create endpoint | Custom container or copy of a saved definition; arguments, environment, CPU/memory, GPU preferences, region selection, scaling, optional registry/weights/images settings |
| Jobs | Search, status/region filters, duration, logs, attempts |
| Create job | Custom `container-run` image or saved definition, declared parameters and uploads, hardware, automatic or pinned placement, priority, timeout, scratch storage |
| Job detail | Overview with monitoring; Metrics, Logs, Attempts, Results, Settings; cancel, fresh submission, explicit checkpoint resume |
| Saved definitions | Reusable endpoint/job configurations; edits and deletion for API-managed definitions |
| API keys | Tenant keys for administrators; own key for other users; budgets, allowed definitions, one-time key display, confirmed revocation |
| Settings / sign-in | API URL and connection test; tenant API key authentication |

Creation and management controls follow the authenticated role from `/v1/keys/me`.
The API continues enforcing authorization. Definitions controlled by fleet configuration
are read-only here. API-managed scaling changes are persisted and reconciled in all
regions, so later definition edits preserve them.

## Scaling controls and remaining deployment support

The endpoint Scaling tab and creation/edit forms use the definitions in
[Cerebrium Scaling Apps](https://cerebrium.ai/docs/scaling/scaling-apps) and its
[TOML reference](https://cerebrium.ai/docs/toml-reference/toml-reference).

- `concurrency_utilization`: target is a percentage of replica concurrency,
  averaged across replicas. Concurrency 200 × target 80% gives 160 requests per
  replica; concurrency 1 × 70% gives 0.7.
- `requests_per_second`: target is a direct average request rate per replica.
  Target 5 means 5 requests/s, without an additional utilization factor or
  concurrency limit.
- `cpu_utilization`: percentage of allocated CPU, averaged across replicas.
- `memory_utilization`: percentage of allocated RAM, excluding GPU memory.
  CPU/RAM scaling requires at least one replica. Both remain disabled here.

New UI policies default to concurrency utilization, target 100%, replica
concurrency 1, cooldown 10 seconds, and evaluation interval 30 seconds. Changing
metric resets the target to its unit-appropriate default. Existing definitions
using `concurrency`, `rps`, or an omitted metric retain their raw Knative target
and utilization factor. The UI labels these as existing policies; it never
reinterprets a concurrent-request count as a percentage. Selecting a new metric
explicitly replaces that policy. Deploy the updated UI and API together.

Checked deployment versions: KServe **v0.20.0**, Knative Serving **1.23** (operator
v1.23.1 bundles Serving v1.23.0), and Envoy Gateway **v1.6.0**. These findings come
from repository and versioned upstream source inspection, without a live cluster.

| Control | Current support and mapping |
| --- | --- |
| Min / max replicas | Editable pod bounds, **separate per regional deployment**. Unlike Cerebrium's app-wide bounds, a maximum of 4 across two regions permits up to 8 pods. Changes persist in the saved definition and reconcile each region. |
| Concurrency utilization | New metric ID maps to native `concurrency`; native `scaleTarget` and `containerConcurrency` both use replica concurrency, while `target-utilization-percentage` uses the UI's percentage target. |
| Fractional concurrency | Supported. Knative 1.23 uses `TargetMin = 0.01`, so replica concurrency 1 and target 70% correctly yield **0.7** requests per replica. The UI shows that fraction without rounding it up to 1. |
| Requests per second | New metric ID maps to native `rps`; native target is the requested rate, utilization annotation is explicitly **100%**, and container concurrency is **0** (unlimited). No extra percentage discount. |
| Replica concurrency | Editable hard simultaneous-request cap for concurrency utilization, 1–1000. Disabled in the new RPS mode because no concurrency cap is enforced there. Legacy modes preserve their existing limits. |
| Cooldown period | `cooldown_s` → `autoscaling.knative.dev/scale-down-delay`, 0–3600 seconds. Reduced demand must persist before scale-down; scale-up remains available. Actual timing also depends on averaging and termination. |
| Evaluation interval | The reference term means the **metrics averaging window**. `window_s` → `autoscaling.knative.dev/window`, 6–300 seconds, default 30 for new policies. Legacy windows retain their wider 6–3600-second runtime range. Knative's separate internal 2-second tick is not this setting; panic/burst handling can react sooner. |
| Response grace period | One editable duration maps to predictor `timeout`; KServe sets request and first-response timeouts, and Knative sets pod `terminationGracePeriodSeconds` to the same duration. Supported here: 1–600 seconds. HTTP timeout does not itself terminate work inside the container; the app needs cancellation and SIGTERM handling. The API invocation timeout and 630-second gateway limit need alignment before longer values can be supported. |
| Idle retention | `idle_s` retains the last pod after deciding to scale to zero, separately from cooldown and evaluation interval. |

Disabled controls are visible reminders and are not submitted as working settings:

- [ ] **Scaling buffer (extra app replicas):** fixed extra ready app replicas
  above the autoscaler's recommendation for concurrency/RPS modes. Requires an
  autoscaler/controller extension with bounds and scale-to-zero behavior defined.
- [x] **Shared warm GPU buffer:** a pool setting of the fleet, not of one app:
  `pools.<name>.warm_nodes` keeps spare nodes with no model on them, shared by
  every model of the pool's GPU class in that region (placeholder pods of negative
  priority that any real pod preempts; `docs/FLEET.md` "Warm spare nodes"). The
  console does not edit fleet settings; the control stays disabled here and points
  to the tfvars. Warm GPU capacity and ready application replicas are distinct
  resources.
- [ ] **CPU utilization:** select HPA, ensure a resource metrics API is available,
  validate percentage targets and minimum replicas, and disable KPA-only controls.
  The bundled HPA extension is not selected by current endpoint definitions.
- [ ] **Memory utilization:** pinned Knative HPA accepts average memory in MiB.
  Convert the percentage against configured pod RAM before rendering that target,
  or add a percentage resource-metric integration. Do not pass a percentage as MiB.
- [ ] **Per-app load balancing:** `round-robin`, `first-available`,
  `min-connections`, `random-choice-2`. Min-connections counts in-flight requests;
  random-choice-2 compares two randomly sampled replicas. Envoy LeastRequest is a
  candidate for the latter, but not a full-scan minimum. Pure Random does not
  implement two-choice routing. The chart/API currently create no per-app policy;
  all forwarding paths, including API/LiteLLM and Knative Activator, need coverage.

Primary runtime references:
[pinned target resolver](https://github.com/knative/serving/blob/knative-v1.23.0/pkg/reconciler/autoscaling/resources/target.go),
[pinned fractional target minimum](https://github.com/knative/serving/blob/knative-v1.23.0/pkg/apis/autoscaling/register.go),
[concurrency and utilization](https://knative.dev/docs/serving/autoscaling/concurrency/),
[scale bounds and windows](https://knative.dev/docs/serving/autoscaling/scale-bounds/),
[KPA/HPA differences](https://knative.dev/docs/serving/autoscaling/autoscaler-types/),
[pinned memory target](https://github.com/knative/serving/blob/knative-v1.23.0/pkg/reconciler/autoscaling/hpa/resources/hpa.go),
[internal tick](https://github.com/knative/serving/blob/knative-v1.23.0/pkg/autoscaler/scaling/multiscaler.go),
[shutdown grace](https://github.com/knative/serving/blob/knative-v1.23.0/pkg/reconciler/revision/resources/deploy.go#L306),
[KServe timeout mapping](https://github.com/kserve/kserve/blob/v0.20.0/pkg/controller/v1beta1/inferenceservice/reconcilers/knative/ksvc_reconciler.go#L207),
and [Envoy load balancing](https://gateway.envoyproxy.io/v1.6/tasks/traffic/load-balancing/).

## Monitoring

Metrics and logs are rendered inside the resource pages. The browser calls authenticated
resource routes on the fleet API; it never contacts Prometheus, Loki, or Grafana directly.
The API resolves the resource, applies model or operation ownership checks, constructs
fixed queries, and forwards worker-region requests with the caller's bearer key.

Metric panels include request rate, p95 latency, errors, replicas, concurrency, CPU,
memory, and GPU usage where available. Missing series remain missing. Time ranges run
from 15 minutes to 7 days; auto-refresh is optional. Completed jobs use completion time
as the historical range end. Logs support literal text search, bounded latest entries,
download, and refresh. Partial metric failures preserve the remaining panels.

Each regional API defaults to:

```text
PROMETHEUS_URL=http://kube-prometheus-stack-prometheus.monitoring.svc:9090
LOKI_URL=http://loki.monitoring.svc:3100
```

Override those variables for different service names. `REGION_API_URLS` connects the
control API to regional APIs, as for inference forwarding. Monitoring needs both the
updated API and UI images; deploying only the UI will show an unavailable state for
these routes. Historical data depends on collection and retention in each region.

## Develop and validate

```sh
cd ui
npm ci
npm run dev
npm run build
```

Vite proxies same-origin `/api` to `VITE_DEV_API` (default `http://127.0.0.1:8080`).
The production nginx image proxies `/api` to `API_UPSTREAM`. Settings or `VITE_API_BASE`
can override the base. Runtime `/config.json` preserves the existing deployment contract.
Fleet regions/classes are fetched from `/v1/fleet`; no region list is compiled into the UI.
The chart bundle is loaded when monitoring is displayed.

API validation, including monitoring authorization, bounds, regional forwarding,
normalization, scaling persistence, and typed container environment/arguments:

```sh
cd services/api
pip install -r requirements-dev.txt
pytest -q
```

The existing image build and platform deployment flow remain in `tools/images.sh` and
the platform stage. The older screenshots in `screenshots/` predate this redesign.
