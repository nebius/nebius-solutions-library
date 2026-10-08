# Serverless 2.0 UI

Customer console in the style of the Nebius Serverless AI console: dark
sidebar, white content, cards and tables. React + Vite + TypeScript, plain CSS
with the Nebius tokens (lime `#daff33`, violet `#5d52f6`, lavender `#c1c1ff`,
neutral greys). No UI framework, no router library, no SSR, no mock data: every
page talks to the customer API (`services/api`).

Served at `https://app.<control cluster host>` by the platform stage (`ui-app` on the control cluster).

## Pages

| Page | What it shows | API |
|---|---|---|
| Login | Paste a LiteLLM virtual key; stored in `localStorage`, sent as `Authorization: Bearer`; one authenticated `GET /v1/models` tells a wrong URL from a rejected key | `GET /v1/models` |
| Models | Catalog cards: name, mode(s), GPU, protocol, measured cold start, price, status per region; "Run job" / "Deploy endpoint" | `GET /v1/models` |
| Jobs | Run/async operations: status, priority, region, started, duration, attempts, cost; filters; auto-refresh | `GET /v1/operations` |
| Job detail | Attempts timeline (preemption, node, resume), input, result artifacts (presigned), logs/metrics links, cancel, resubmit | `GET /v1/operations/{id}`, `/result`, `:cancel` |
| New job wizard | Name, catalog model or own image (command/args/env), parameters from the model's declared `parameters` (files upload via `POST /v1/artifacts/uploads`), scheduling (region, priority, timeout), review. Pool, image, retries and checkpointing come from the model's run class; `input` carries only parameters the WorkflowTemplate declares (the API rejects unknown keys) | `POST /v1/models/{m}:invoke` |
| Endpoints | Sync/async models per region: status, replicas, scale-to-zero idle, GPU, placement, managed by (git/api), last cold start | `GET /v1/endpoints` |
| Endpoint detail | "Try it" box (one sync invoke with timing and response), configuration, edit scaling, delete | try-it = `POST ...:invoke` `{mode: sync}`; `PATCH /v1/endpoints/{id}` (admin key; git-managed ones are reverted by Argo CD); `DELETE` (403 for git-managed) |
| Deploy endpoint | Model, region, placement, min/max replicas, idle timeout, concurrency, auth | `POST /v1/endpoints` (admin key; model needs a catalog runtime; 409 if it exists) |
| API keys | Keys of the tenant with role, budget, spend meter, model allow-list, expiry; create (alias, budget, models, expiry) shows the key once; revoke | `GET/POST /v1/keys`, `DELETE /v1/keys/{alias}` (admin key) |
| Settings | API URL, probe | |

Replica history and per-endpoint metrics are not drawn here: the Metrics and
Logs buttons open Grafana (dashboards in `clusters/common/manifests/observability`).

## Develop

```sh
cd ui
npm install
npm run dev          # http://127.0.0.1:5173
npm run build        # tsc --noEmit + vite build -> dist/
```

The default API base is same-origin `/api`: in the image nginx proxies it to
`http://api.api.svc.cluster.local` (env `API_UPSTREAM`, `DNS_RESOLVER`), in
`npm run dev` Vite proxies it to `VITE_DEV_API` (default `http://127.0.0.1:8080`),
so the browser never needs CORS. `VITE_API_BASE` (build arg) or Settings override it.
Fleet-specific addresses are never compiled in: nginx serves `/config.json` from the
pod's environment (`PUBLIC_API_URL`, `GRAFANA_URLS` in the API's `region=url,...`
format; `src/config.ts` loads it before the first render). The API client lives in
`src/api/client.ts`; shapes in `src/api/types.ts` follow `services/api/openapi.yaml`.

## Ship

`tools/images.sh build` builds and pushes it with the other platform images to the
fleet's registry (tag from `terraform.tfvars` `images.versions.ui`); the platform
stage deploys nginx (unprivileged, port 8080) in namespace `ui-app` on the control
cluster with an HTTPRoute on the `serverless2-external` gateway, host
`app.<control cluster host>`. Current tag: `0.2.2` (runtime configuration).

## Screenshots

`screenshots/` (1440x900, playwright, deployed site with the demo tenant key):
`20-live-models`, `21-live-jobs`, `22-live-endpoint-tryit`, `23-live-endpoints`,
`24-live-keys`, `26-live-endpoint-git`. Taken with UI 0.1.x; the job wizard and
job detail have fewer fields since 0.2.0.
