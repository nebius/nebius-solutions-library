# Gateway, authentication and certificates

Envoy Gateway is installed with its upstream Helm chart; Knative's Gateway API integration creates HTTPRoutes for KServe predictors. Shared gateway manifests are in `clusters/common/manifests/gateway`, with per-cluster patches in `stack/platform/stage.tf`.

## Public and private routing

`edge.mode = "public"` allocates a static public gateway address. With `edge.domain`, service hosts are `<service>.<cluster>.<domain>`; without it they use `<service>.<gateway-ip>.sslip.io`. Configure real DNS for production and set `edge.acme.email`. ACME staging is available for installation tests. The HTTP listener accepts certificate challenges; application traffic uses HTTPS. `edge.source_cidrs` restricts the HTTPS listener. `edge.grafana_public = false` exposes Grafana through the internal gateway.

`edge.mode = "internal"` requires a real `edge.domain`, `edge.private_ca_secret` and a complete `trust_bundle_pem`. The CA Secret (`tls.crt` and `tls.key`) must already exist in `cert-manager` on every cluster. Terraform installs a private CA ClusterIssuer and excludes public ACME issuers. DNS must resolve the configured names to the internal load balancer and clients must trust the CA. No fabricated `0.0.0.0.sslip.io` hostname or insecure verification fallback is used.

Predictor hosts are `<model>-predictor.models.<cluster-domain>`. The API reconciles the regional models Certificate when definitions change. Terraform ignores its API-owned SAN/issuer fields. The first-use placeholder is self-signed; wait for real issuance before testing externally. Do not use a TLS verification bypass as a production workaround.

## Endpoint authorization

`charts/endpoint` renders a native SecurityPolicy per endpoint. It selects that predictor's external HTTPRoutes and binds the model ID in the ext-auth path `/internal/authorize/<model>`. Envoy appends the original request path and forwards the authorization/WebSocket headers; it does not forward the request body. A ReferenceGrant permits the policy to reference Service `api` in namespace `api`.

The API's shared authorizer checks tenant binding, key expiry/blocking, model permissions and budget. It returns only tenant and key-alias headers to the predictor. A misleading forwarded host cannot select another model. Missing/forbidden keys are denied; unavailable authorization fails closed. There is no separate edge-auth deployment, embedded Python ConfigMap or duplicate key cache.

Supported key transports are `Authorization: Bearer <key>`, `api_key` query parameter, and a WebSocket subprotocol `bearer.<key>` (or the raw key). Prefer the authorization header; query keys can be exposed by client/proxy URL logging. Do not log credentials. The UI reaches endpoints through the API and displays monitoring inside the endpoint/app.

LiteLLM model groups use a platform-internal tenant key when calling protected predictor hosts. It must be present for OpenAI group registration; a missing key is reported in model-write warnings. Native Envoy rate limits and LiteLLM proxy rate/budget checks share the existing Redis service. See `OPERATIONS.md` for its availability and accounting limits.

## Network and lifecycle checks

Tenant policies permit gateway/API access and the existing artifact path, while restricting direct predictor/cross-tenant access. Upstream internal cluster services still require a trusted Kubernetes boundary; gateway auth is not a replacement for tenant network/RBAC isolation.

Validate policy attachment, an allowed key, another model's key, an exhausted key, a blocked key, a WebSocket handshake and an untrusted certificate on a test fleet. Check both regional and control API paths. Gateway proxy replicas and disruption budgets are configured in the shared gateway manifests; test node-drain behavior against the customer's availability requirement.

Image pre-pull, Zot/Spegel caching and shared weights remain independent of gateway policy; see `IMAGES.md` and `charts/endpoint/README.md`.

## Certificates of the GPU regions: the fleet CA (2026-10-10)

The control cluster's public hostnames (`api`, `app`, `grafana`, `litellm`) get Let's Encrypt certificates
through ACME HTTP-01 on its gateway. The GPU regions' gateways (`api.<region>`, `grafana.<region>`, the model
endpoints) get theirs from the **fleet CA** by default (`edge.regional_certificates = "fleet-ca"`): the cloud
stage generates a CA key pair, the platform stage installs it as cert-manager's `fleet-ca` ClusterIssuer on
every GPU region and puts the CA certificate into the `trust` ConfigMap, the API merges it with the system
roots (`services/api/config.py`) and LiteLLM reads the bundle through `SSL_CERT_FILE`. Nothing in the fleet
then depends on a regional gateway being reachable from the public internet, which an ACME challenge needs:
two regions added on 2026-10-10 (eu-west1, eu-north2) answered on port 80 only from inside Nebius, Let's
Encrypt timed out, and the control plane could not forward to them over HTTPS. A browser on a regional
Grafana sees a certificate warning (operator use); the console and the API talk to the control cluster
only. `edge.regional_certificates = "acme"` restores per-region Let's Encrypt where the gateways are public.

Rotation: when the fleet CA changes (a new fleet, or the cloud state rewritten from another checkout), the
platform stage re-issues every region's leaf certificates (`terraform_data.reissue_on_ca_change`), LiteLLM
restarts on the changed bundle, and the API rebuilds its trust file before each cross-cluster call. Apply the
control cluster first, then the regions. One rule for operators: every `./stack.sh apply` of one fleet must run
from the same checkout at the same commit; an older copy of the stack silently drops resources it does not know
(the fleet CA on 2026-10-10).
