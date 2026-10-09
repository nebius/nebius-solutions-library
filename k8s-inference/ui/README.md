# Serverless AI standalone console

React, TypeScript and Vite application for the inference fleet's customer API.
It uses [Gravity UI](https://github.com/gravity-ui/uikit) controls, a Nebius theme,
bundled Inter fonts and the Nebius wordmark. The UI runs independently of the
Nebius Cloud console and authenticates with a tenant API key.

The shell provides a fleet/tenant context, resource navigation, breadcrumbs and
responsive navigation. Endpoints is the landing page. The tables, full-page
forms, detail tabs, dialogs and empty states share the same components and theme.

## Workflows

| Page             | Behavior                                                                                                                                                                    |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Sign in          | Validates the key with `GET /v1/keys/me`; masks the key by default.                                                                                                         |
| Endpoints        | Searches and filters live endpoint state across connected regions. Links carry both endpoint ID and region.                                                                 |
| Endpoint details | Overview, configuration and a JSON request tester for HTTP/OpenAI compatible endpoints. Live scaling is available to administrators.                                        |
| Create endpoint  | Full-page container, resource, networking and autoscaling form. Creates the model with `POST /v1/models`.                                                                   |
| Jobs             | Searchable latest 100 operations, status filters and automatic refresh.                                                                                                     |
| Job details      | Overview and attempts, input, results/downloads, cancellation and resubmission.                                                                                             |
| Create job       | Declared model parameters, optional file uploads, region or automatic fleet placement, priority and timeout. For a custom image, select the built-in `container-run` class. |
| Models           | Searchable endpoint/job-class definitions. Administrators can create, edit and delete API-managed models. Built-in definitions remain read-only.                            |
| API keys         | Administrators manage tenant keys, budgets, model allow-lists and expiration. Members see their own key. New secrets are shown once; revocation requires confirmation.      |
| Settings         | Tests a changed API URL before saving it and shows account/region information.                                                                                              |

Model writes use `/v1/models`; there are no endpoint creation/deletion API routes.
Editing preserves advanced spec fields that are not exposed in the form, including
existing argument-vector commands and weight mounts. Environment values retain
spaces and `=` characters.

The UI uses `GET /healthz` for connected regions and available GPU classes, and
`GET /v1/keys/me` for the tenant and administrator role. Endpoint list, detail and
scaling requests accept an optional `region` query. Deploy the API and UI from the
same solution revision to get these contracts together.

Metrics and logs open the configured Grafana instance. Missing in-flight or cold
start measurements are not replaced by estimates. The JSON tester does not
support gRPC or WebSocket protocols.

## Develop and verify

Use Node 22.12 or later (the container uses Node 22).

```sh
cd ui
npm ci
npm run dev
npm test
npm run build
```

The development server runs at `http://127.0.0.1:5173`. Its same-origin `/api`
proxy targets `VITE_DEV_API`, defaulting to `http://127.0.0.1:8080`. A tenant
key is required; production code has no demo data or mock API.

Interaction tests cover role restrictions, regional links and reads, job
submission, editing payload preservation, session handling, and dialog keyboard
behavior. API tests cover regional state, scaling authorization and cache
isolation. CI runs the UI tests/build separately from the solution's existing
validation and image scan.

## Runtime configuration and deployment

The unprivileged nginx image serves the application on port 8080 and proxies
`/api` to `API_UPSTREAM`. Set `DNS_RESOLVER` for the cluster. The platform stage
serves it at `https://app.<control cluster host>`.

nginx generates `/config.json` from `PUBLIC_API_URL` and `GRAFANA_URLS`
(`region=url,...`). The UI loads it before rendering, so the same image works
for different fleets. Fleet addresses are not compiled into the bundle.
`VITE_API_BASE` or Settings can override the API base.

The API key and API URL are stored in this browser's local storage. Sign out
removes the key. Existing deployment configuration can replace live scaling
patches; edit the model to persist replica limits across redeployments.

## Visual assets

Inter is self-hosted through `@fontsource/inter` (SIL Open Font License).
`public/nebius.svg` is the official wordmark from the Nebius documentation
logo asset:
[official asset](https://mintcdn.com/nebius-ai-cloud/coWpUI3da21fpBbP/logo/logo.svg).
The application does not load fonts or branding assets from a CDN at runtime.

Live comparison against the Nebius Cloud console and browser screenshot checks
are still required before claiming an exact visual match. No screenshots from
the Cloud console are included in this repository.
