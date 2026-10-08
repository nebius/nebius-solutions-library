# Edge: hostnames, TLS and the key check (lane H)

*For operators: how traffic enters a cluster (gateway, TLS, key checks, rate limits, hostnames).*

What is deployed today and what changes when a real domain exists. No DNS
action is taken in phase 1; this is the plan and the pieces that are already
in place. Manifests: `clusters/common/manifests/edge/` (ext-auth, rendered by
`spikes/S15-edge/render.py`) plus `clusters/<cluster>/apps/overlays/edge/` (pre-pull,
LiteLLM address patch); gateways in `clusters/common/manifests/gateway/` plus
`clusters/<cluster>/apps/overlays/gateway/` (hostname, certificate).
Numbers: `spikes/S15-edge/RESULT.md`.

## Today (sslip.io hostnames, public ACME certificates)

| Cluster | Gateway LB | Hosts | TLS |
|---|---|---|---|
| hub (eu-north1) | `<hub ip>` | `api.`, `app.`, `argo.`, `argocd.`, `grafana.`, `litellm.` and `<model>-predictor.models.` on `<hub ip>.sslip.io` | `hub-wildcard-tls`: one certificate with every hostname as a SAN, ACME HTTP-01 (ClusterIssuer `letsencrypt`), `clusters/hub/apps/overlays/gateway/tls.yaml` |
| eu-south1 | `<region ip>` | `api.`, `argo.`, `argocd.`, `grafana.` and `<model>-predictor.models.` on `<region ip>.sslip.io` | `region-wildcard-tls`, same scheme, `clusters/eu-south1/apps/overlays/gateway/tls.yaml` |

Both gateways (`envoy-gateway-system/serverless2-external`, Envoy Gateway
v1.6) have an `http` (80) and an `https` (443, `*.<ip>.sslip.io`) listener;
`serverless2-internal` (ClusterIP) carries the Knative cluster-local routes
that LiteLLM and the customer API use. Knative's `config-domain` is
`<ip>.sslip.io`, so a KServe endpoint `m` in namespace `models` is
`m-predictor.models.<ip>.sslip.io` (KServe 0.20 Knative mode only publishes
the predictor route).

Certificates: cert-manager's Gateway API solver answers the ACME HTTP-01
challenge with an HTTPRoute on the `http` listener
(`clusters/common/manifests/gateway/acme.yaml`, cert-manager value
`config.gatewayAPI.enabled`). Wildcards need DNS-01 and therefore a real
domain, so every public hostname is listed as a SAN; adding a model or a
route on a cluster means adding its hostname to that cluster's `tls.yaml`.
The Secret names did not change, so the listener kept serving the previous
(self-signed) certificate until the first issuance. `sslip.io` is not on the
Public Suffix List: Let's Encrypt's 50-certificates-per-registered-domain
week is shared by everyone who uses sslip.io. If `kubectl -n
envoy-gateway-system describe certificate` shows "too many certificates
already issued", cert-manager retries by itself (the weekly bucket moves) and
nothing breaks in the meantime; a rate-limit adjustment can be requested from
Let's Encrypt. Buypass was tried as a second CA: its ACME service is gone. No ACME account
email is configured (optional; renewal is automatic 30 days before expiry).

Regional API: `api.<ip>.sslip.io` exists on both clusters
(`clusters/common/manifests/api` + overlays). Sync, async and streaming calls
for an endpoint go to the API of the region that hosts it (the hub API
answers 400 for another region's endpoints); runs are submitted through the
hub API for every region. The regional instance validates keys against the
hub's LiteLLM over `litellm.<hub host>` with certificate
verification, as does eu-south1's edge-auth.

Rate limits: per API key (distinct `Authorization` header), counted in a
shared Redis by the Envoy Gateway rate-limit service: 600 requests per
minute on the API route (`api/api-rate-limit`), 3000 per minute on every
other external route, i.e. the predictor hosts (`envoy-gateway-system/per-key-rate-limit`,
Gateway-level). Requests without an `Authorization` header (browser UIs) are
not counted. Redis down means fail-open.

## Key check on the predictor hosts

Anyone who knew a predictor host could call the model directly, bypassing
LiteLLM. Now every external Knative route in `models` carries an Envoy
`SecurityPolicy` (`models/model-key-check`, selected by labels
`serving.knative.dev/route` exists and `networking.knative.dev/visibility`
not `cluster-local`, so new endpoints are covered automatically) with an
HTTP ext-auth backend `edge-auth` (2 replicas, `spikes/S15-edge/edge_auth.py`,
116 lines of stdlib Python shipped in a ConfigMap on `python:3.12-alpine`,
no image build, no registry mirroring).

How a request is checked:

1. Envoy sends method, path (with query string) and the headers
   `authorization`, `sec-websocket-protocol`, `x-forwarded-host`,
   `x-forwarded-for`, `user-agent` to `edge-auth` (`/check<path>`), body never.
2. `edge-auth` takes the key from, in order: `Authorization: Bearer <key>`;
   `?api_key=<key>`; `Sec-WebSocket-Protocol: bearer.<key>` (or a bare `sk-…`
   token in that header).
3. It validates the key by calling LiteLLM `GET /key/info` **with that key as
   the bearer** (a key may read its own info; LiteLLM's own auth rejects
   unknown, blocked, expired and over-budget keys), so no master key is
   distributed to the regions. hub: `http://litellm.litellm.svc:4000`;
   a region: `https://litellm.<control host>` with TLS verification
   off until a real certificate exists (`LITELLM_TLS_VERIFY=false`).
4. Decisions are cached per key hash (30 s allow, 10 s deny). `200` adds
   `x-serverless2-tenant` / `x-serverless2-key-alias` to the upstream request;
   `401` (missing or unknown key, `WWW-Authenticate: Bearer`), `403` (refused:
   blocked, budget), `503` when LiteLLM is unreachable (`failOpen: false`).

What is not policed: the cluster-local routes (`*.models.svc.cluster.local`
through `serverless2-internal`), which is how LiteLLM pass-through routes and
the customer API (and therefore the console's try-it box) reach the models;
they authenticated the caller already. Routes outside `models` (the S2 spike
`s2-qwen-predictor.spikes…`) are not covered; the API/UI/operator hosts keep
their own logins.

### WebSocket (live STT) and keys

Envoy runs ext-auth on the HTTP upgrade request, so the WebSocket handshake
is where the key is checked; after `101` the stream is untouched. Clients:

| Client | How |
|---|---|
| SDKs, `websockets`, curl, server-side code | `Authorization: Bearer <key>` header on the handshake (`stt_ws.py -H "Authorization: Bearer <key>"`) |
| Browser `new WebSocket(url)` (cannot set headers) | `wss://<host>/v1/audio/stream?api_key=<key>`; the key stays in the URL of the handshake only (TLS), it is never logged by `edge-auth` |
| Browser alternative | `new WebSocket(url, ["bearer.<key>"])`; **the STT runtime does not echo a subprotocol**, so browsers abort the handshake after `101` (checked with a raw upgrade: `101` without `sec-websocket-protocol`). Accepted by the gateway, usable by non-browser clients; for browsers the runtime would have to accept the subprotocol |

Scale-from-zero still works: a rejected handshake never reaches the activator
(no GPU wake-up for unauthenticated traffic), an accepted one is buffered by
the activator as before.

## When a real domain exists

Assume `serverless.example.com` (the `<domain>`). The scheme is
`<model>.<region>.<domain>` for endpoints and `<service>.<domain>` for the
control plane on the hub:

| Record | Value | Serves |
|---|---|---|
| `*.eu-north1.<domain>` A | `<hub ip>` | `nemotron-speech-en-0-6b.eu-north1.<domain>`, `diffdock.eu-north1.<domain>`, … |
| `*.eu-south1.<domain>` A | `<region ip>` | `nemotron-speech-en-0-6b.eu-south1.<domain>` |
| `api`, `app`, `litellm`, `argo`, `argocd`, `grafana` `.<domain>` A | `<control ip>` | customer API, console, operator UIs |

Changes, all in git, per cluster:

1. **Knative** (`clusters/common/manifests/knative/knative-serving.yaml`, domain patched per cluster in `overlays/knative-serving`):
   `config-domain` becomes `eu-north1.<domain>` / `eu-south1.<domain>`, and
   `config-network` gets `domain-template:
   "{{index .Labels \"serverless2.nebius/model\"}}.{{.Domain}}"` (the endpoint
   chart already labels every InferenceService, Route and HTTPRoute with
   `serverless2.nebius/model`), which drops the `-predictor.models` infix and
   yields `<model>.<region>.<domain>`. Knative rewrites the HTTPRoutes; the
   `SecurityPolicy` selects by label, nothing to change there.
2. **Certificates**: the same ClusterIssuers with a DNS-01 solver for the domain's DNS provider replace the HTTP-01 SAN lists with one wildcard Certificate per cluster:
   - Preferred: one **wildcard per region** via ACME **DNS-01**
     (`*.eu-north1.<domain>` on the hub plus `*.<domain>` for the control
     plane, `*.eu-south1.<domain>` in the region). Wildcards are DNS-01 only
     (Let's Encrypt does not issue wildcards over HTTP-01); this needs the
     zone at a provider cert-manager can drive (Route 53, Cloudflare, Google
     Cloud DNS, Azure, RFC 2136, or a webhook) with a credential Secret per
     cluster. One `ClusterIssuer` + one `Certificate` per region replace the
     `selfsigned` Issuer; nothing per model.
   - If the zone cannot be driven by cert-manager: ACME **HTTP-01 through the
     Envoy gateway**, one Certificate per hostname. Enable the Gateway API
     solver in the cert-manager Application (`config.enableGatewayAPI: true`
     in `clusters/common/values/cert-manager.yaml`), `ClusterIssuer` with
     `solvers[0].http01.gatewayHTTPRoute.parentRefs: [{name: serverless2-external, namespace: envoy-gateway-system, sectionName: http}]`
     (cert-manager creates a temporary HTTPRoute on the `http` listener for
     `/.well-known/acme-challenge/…`; port 80 stays open for that), and the
     catalog renderer emits a `Certificate` per model host next to the
     InferenceService. The `https` listener then lists one `certificateRefs`
     entry per Secret (Envoy serves by SNI), or one listener per hostname.
3. **Gateways**: the `https` listener `hostname` becomes `*.eu-north1.<domain>`
   (hub also a second `https-control` listener on 443 for `*.<domain>`) with
   the new Secret names; the `http` listener stays for ACME and gets an
   `HTTPRoute` filter `RequestRedirect` to https for everything else.
   Set `LITELLM_TLS_VERIFY` to `true` and `LITELLM_URL` to `https://litellm.<domain>`
   in `clusters/eu-south1/apps/overlays/edge/kustomization.yaml`.
4. **Hosts in code/manifests**: `api.<domain>` (`clusters/hub/apps/manifests/api/api.yaml`
   HTTPRoute and `ARGO_UI_URL*`), `app.<domain>` (`ui-app.yaml`,
   `ui/src/api/client.ts` `PUBLIC_API_URL`), `ui/routes.yaml` for the
   operator UIs, `catalog` docs and `services/api` responses that print
   endpoint URLs. LiteLLM model routes keep the cluster-local hosts.
5. **Hub-only control plane**: `api.<domain>` is one API instance on the hub
   that already holds a kube client per region; regional endpoints are reached
   by their own regional hostname, runs by `region` in the request.

Nothing above requires a different gateway or ingress; the self-signed
Issuers are replaced by ACME ones and the hostnames change. Until then,
clients use `-k` / `verify=False` against the sslip.io hosts.

## Image pre-pull per GPU pool (known issue 10)

`clusters/<cluster>/apps/overlays/edge/prepull.yaml`: one DaemonSet per pool
(`nodeSelector serverless2.nebius/pool`, GPU toleration, no GPU request) with
one init container per catalog image deployed on that cluster (per-region
image from `deployments.<cluster>.image`, pull secrets from the catalog,
interactive `coldStartClass` first) that runs `sh -c echo pulled` and a
`pause:3.10` main container. A new spot node starts pulling the moment it
joins, before the first endpoint pod is scheduled there; rendering reads
`catalog/models/*.yaml`, so onboarding a model re-renders the DaemonSets
(`python3 spikes/S15-edge/render.py`). Measured head start and before/after
cold starts: `spikes/S15-edge/RESULT.md`. DiffDock's NGC weights are not
cached by this (see RESULT: LocalModelCache needs the NIM cache in object
storage first).

## Terraform solution (2026-10-08): `edge` inputs

`edge.mode = public` gives every cluster a static public IP (`nebius_vpc_v1_allocation`, annotated on
the Envoy Service so re-creating it keeps the address); `internal` makes the gateways private load
balancers (no ACME). `edge.domain = null` keeps the `<service>.<ip>.sslip.io` hostnames with HTTP-01
certificates per hostname; a domain gives `<service>.<cluster id>.<domain>` (A records from the outputs,
same ACME path). `edge.source_cidrs` is a SecurityPolicy allow-list on the HTTPS listener (the HTTP
listener stays open for ACME); `edge.acme.email`/`staging` set the issuers; `edge.api_rate_limit_per_minute`
the per-key limit; `edge.grafana_public = false` moves Grafana to the internal gateway.
`edge.ip_certificate` requests a Let's Encrypt IP-address certificate (profile `shortlived`) on the gateway
IP in addition; it is rendered but was not exercised on the test fleet.
