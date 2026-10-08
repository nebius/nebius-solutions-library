# Security pre-review (2026-10-08, main b24ebfd, live fleet control/hub/eu-south1)

> The ids, hostnames and IP addresses in this record belong to the reference fleet the solution was developed and measured on; they are evidence, not inputs. A fleet you deploy has its own.

*For the security review and the owner: findings against the Nebius checklist, what is fixed, what is accepted.*

Self-assessment of Serverless 2.0 against the Nebius Platform Security checklist "Prepare design for
security review" (Basics + authentication, authorisation, networking, encryption, tenant isolation, logging,
secrets, user input, sensitive data, availability/backups, third-party dependencies, vulnerability
management) and the categories Platform Security raised in the Forge review (SECENG-1689: external auth
mechanism, static cross-cluster credentials, audit trails, egress/DDoS, encryption at rest, patch coverage).
Every row is sourced from the repository or the live clusters (read-only). Nothing was changed.

Two deployment targets matter for severity:

- **Fleet as operated today**: several tenants (keys) of one organisation on one fleet.
- **Library target (lane H2)**: one customer per fleet in the customer's own project(s), deployed and
  possibly managed by a Nebius architect. Cross-*customer* isolation then comes from Nebius projects; the
  findings below marked *tenant* become *team* isolation inside one customer, one level less severe.

Severity is the rank Platform Security would most likely give; "Gate" says whether the finding must be closed
before a customer-facing (Preview/GA) launch.

## 1. Findings

| # | Sev | Gate | Domain | Finding (where) | Why it is flagged | Leanest remediation |
|---|---|---|---|---|---|---|
| F1 | High | yes | Tenant isolation / input | `container-run` executes an arbitrary image and command as whatever user the image sets (root by default) on shared GPU nodes: `services/api/jobs.py` renders the `main` container without `runAsNonRoot`, seccomp, dropped capabilities, read-only root; tenant namespaces carry no Pod Security Admission labels (live: `tenant-demo`, `tenant-eval`: none). The pod shares its PID namespace with the uploader (`shareProcessNamespace: true`), whose environment holds the tenant's S3 keys and whose token is the `job-runner` ServiceAccount. | Arbitrary code as root on a node shared with other tenants' pods (and with the endpoint pods in `models`) is the classic multi-tenant escape surface; Platform Security asked for "execution: confine/harden env, isolation". Within one tenant the PID sharing only exposes that tenant's own credentials (the main container already receives them), so the cross-tenant part is the node. | Keep Kubernetes isolation but harden the rendered pod: `runAsNonRoot` where the image allows (parameter), `seccompProfile: RuntimeDefault`, `capabilities.drop: [ALL]`, `allowPrivilegeEscalation: false`, PSA `baseline` label on tenant namespaces (defence in depth; the API never renders privileged pods, PSA proves it). For managed multi-customer fleets: dedicated GPU pools per tenant (one Kueue ResourceFlavor + node label per tenant, no code) or one fleet per customer (the H2 target). Kata/gVisor are not offered on mk8s; say so in the design doc. |
| F2 | High | yes | Authorisation / billing | Tenant run pods may call every model endpoint directly inside the cluster: `tenant-isolation` CiliumClusterwideNetworkPolicy allows egress to `models`, `knative-serving`, `envoy-gateway-system`, and async jobs use `http://<model>-predictor.models.svc.cluster.local` (`services/api/catalog.py:50`). The gateway `SecurityPolicy model-key-check` (ext-auth, key check, LiteLLM spend) and the per-key rate limit apply only to the external HTTPRoutes. | A tenant's job can invoke any endpoint, including models its key is not allowed to use, without authentication, without per-key rate limiting and without LiteLLM accounting (billing bypass). In a one-customer fleet this is an accounting gap; with several tenants it is an authorisation bypass. | Route the async call through the gateway with the caller's key (the per-operation `-auth` Secret already exists; `call.sh` only needs the external URL), then drop `models`/`knative-serving` from the tenant egress rule (keep `envoy-gateway-system` for the external hostname, or the Knative internal gateway only for the runner image). One policy change, one URL change. |
| F3 | High | yes | Vulnerability management | No image or dependency scanning anywhere (no CI, no Semgrep/trivy/grype), no documented patch cadence; the stack includes LiteLLM 1.104 (large Python surface, frequent CVEs), KServe 0.20, Knative 1.23, Envoy Gateway 1.6.0 (1.8.3 current), Kueue 0.20, Zot 2.1.22, Spegel 0.7.4, CNPG 0.29. | The checklist requires patchability as a non-functional requirement and scanner coverage (Semgrep on code, container scanning). The review will ask for the patch plan of each big dependency. | Add one GitHub Actions workflow: Semgrep on `services/`, `trivy` on the five own images and the pinned third-party images, Renovate/Dependabot for chart versions; write a "Patching" section in OPERATIONS.md (monthly chart bump, critical CVE within 7 days, the `helm_release` versions in tfvars are the control). Upgrade Envoy Gateway to 1.8.3 in H2's fresh-deploy test. |
| F4 | Medium | yes | Authentication | Operator interfaces are on the public gateway IP with password-only login and no IP restriction: Grafana (`grafana.<ip>.sslip.io`, admin Secret), the fleet Argo CD (`argocd.<ip>`, initial admin Secret, `server.insecure` behind the gateway), the LiteLLM proxy and its admin UI (`litellm.<ip>`, master key), the customer UI (`app.<ip>`, pasted key kept in `localStorage`). | The checklist prefers Nebius IAM or Entra SSO and no password-based auth; the Forge review's first finding was exactly the external-auth mechanism. | Envoy Gateway `SecurityPolicy` with OIDC (the `envoy-oidc-hmac` Secret is already there) against Entra for the operator hostnames, plus a `source_cidrs` allow-list in the edge block (H2 tfvars `edge.source_cidrs`); keep the LiteLLM proxy internal (remove the public route; the API is the customer surface). No code. |
| F5 | Medium | yes | Authorisation / IAM | The ops service account holds `admin` on the whole project (`infra/cluster/ops-admin.tf`: "the SA may change IAM inside its own project"); its keys live in the `ops` namespace of every cluster and in the dispatcher's `price-feed-nebius-sa`. The MultiKueue manager, the fleet API's `api-agent` and the tenant RBAC are correctly scoped (namespace-bound ClusterRole, daily-rotated kubeconfigs). | "Custom IAM roles with minimum permissions; no broad roles". Compromise of any ops CronJob pod = project admin. | Split the identity: an `editor`-scoped SA for node recovery/filesystem/registry pushes, a `viewer` SA for the price feed and backups-read, and move the IAM-changing steps (permits, keys) into Terraform run by the operator (H2 does this). Document which permits remain and why. |
| F6 | Medium | yes | Secrets | Long-lived credentials: the Zot sync Secret holds a Container Registry static key valid up to 3 years (`registry/zot-sync-credentials`, docs/IMAGES.md) and the NGC key; tenant S3 keys are static (rotated only by hand); the LiteLLM master key is a static Secret in two namespaces; Terraform state of a tenant contains the S3 `secret_key` in clear (`infra/state/hub/tenants/eval/terraform.tfstate`, and the same in the `serverless2-tfstate` bucket); copies of every key and the Argo CD deploy key sit unencrypted under `infra/state/` on this host. | "Prefer short-term secrets, avoid long-lived credentials; Mysterybox/KMS for secrets". State files with credentials are a standard review finding. | Zot: use the ops SA credential helper (`ops-login.sh`, per-call token) instead of a static key, or a 30-day key rotated by the existing `rotate-nebius-keys` CronJob. Tenant keys: extend the weekly rotation CronJob to tenant SAs (same script). State: restrict the state bucket to the operators' group (done) plus versioning retention; in H2 create keys with write-only/ephemeral attributes (Terraform >= 1.11) so they never land in state; wipe `infra/state/` on this host after H2's migration. Master key: SecretStash reference in tfvars (H2). |
| F7 | Medium | yes | Logging / audit | No who-did-what trail for the customer API: uvicorn runs without an access log that includes the principal; operations are attributable only through Job labels (`serverless2.nebius/tenant`) and LiteLLM spend rows. Kubernetes audit logs are on (mk8s `audit_logs = {}` -> bucket `sp_mk8s_audit_logs`), but nothing is wired to Audit Trails or a SIEM; Loki keeps 30 days. | Checklist: integrate with Audit Trails or o11y events; all CRUD logged, for all components. | Turn on a structured request log in the API (one middleware line: method, path, tenant, key alias, status, operation id) shipped by Alloy to Loki, retention 90 days for that stream; Argo CD/Grafana/LiteLLM already log logins. For Nebius-managed fleets document the Audit Trails hop as the customer's bucket. |
| F8 | Medium | no (Preview), yes (GA) | Encryption in transit | Plain HTTP inside the clusters: API -> LiteLLM (`http://litellm.litellm.svc:4000`), Zot (`http://127.0.0.1:30500` NodePort), Spegel (`:30021`), Loki push, Prometheus scrapes, Alloy, OpenCost; the edge-auth service and the regional APIs talk to LiteLLM/control over HTTPS (Let's Encrypt). CNPG uses TLS by default. | "Data in transit end-to-end TLS". Cilium is the CNI; no mesh. In-cluster plain HTTP is common and usually accepted when the network policy is tight, but it will be asked. | Enable Cilium WireGuard transparent encryption on the mk8s clusters if the platform allows (one flag, no app change); otherwise document in-cluster HTTP as accepted risk with the network policies as compensating control. Zot/Spegel are loopback/NodePort by design. |
| F9 | Medium | yes (multi-customer), no (one customer per fleet) | Networking / tenant isolation | Zot (`30500`) and Spegel (`30021`) are NodePorts with anonymous read on every node's VPC address; the hub's VPC subnet is shared with other clusters of `project-e00rene` (the FS2 POC cluster). Anyone on the VPC can list and pull every cached image, including NGC NIMs pulled with the platform's NGC key and the platform's own images. | Private images readable without authentication from the network segment; the checklist asks for ACLs between components. | Bind the NodePort to the node's loopback for Zot (containerd pulls from `127.0.0.1`; Spegel needs node-to-node: restrict with a CiliumClusterwideNetworkPolicy to `remote-node` entity) and keep one project/VPC per fleet (the H2 target). |
| F10 | Medium | yes (multi-customer) | Input / authz | The `image` parameter of `container-run` is pulled by the node with the node's registry credential provider: a tenant may name any private image in any registry of the project (`cr.eu-north1.nebius.cloud/<any registry id of the project>/...`), i.e. other teams' images in the same project. `input_prefix` is validated to the tenant bucket (`app.py:131`); `output_prefix` from `input` is only defaulted (`setdefault`), so a tenant can direct its upload to any bucket its own key can write (harmless) or a public one (exfiltration of its own data, harmless). | Image names are user input that reaches a privileged credential (the node's). | Validate `image` against an allow-list of prefixes per tenant in the catalog (`registry.serverless2.local/<alias>/`, public registries) and reject raw `cr.*.nebius.cloud` references unless the tenant owns the registry; one check in `jobs.py`. Make `output_prefix` non-overridable (drop it from accepted input keys). |
| F11 | Medium | yes | Design doc | No security design artefacts: no diagrams in any doc (0 mermaid blocks in ARCHITECTURE/SCHEDULING/EDGE/CONTROL-PLANE/JOBS), no threat list, no data classification, no statement on regulatory scope. | The review starts from the design doc (Basics). | See section 3. |
| F12 | Low | no | Encryption at rest | Nebius Object Storage, network-ssd volumes and the shared filesystem are provider-encrypted at rest (platform statement to be cited); application-level encryption none; LiteLLM keys are hashed by LiteLLM, the master key is stored in clear in Secrets. | The reviewer will ask for the at-rest statement per store. | Cite the Nebius at-rest guarantee for Object Storage / Compute disks in the design doc; no change. |
| F13 | Low | no | Availability | HA 2 replicas and PDBs for the control-plane services, CNPG daily base backup + WAL (14 d), state bucket versioned, restore drill documented (OPERATIONS.md). Single public IP per region without a static allocation (recreating the gateway Service changes it). | Minor; the static IP is in H2's list. | `nebius_vpc_v1_allocation` per gateway (H2). |
| F14 | Low | no | Third-party / privileged components | Privileged or host-level pods: `node-config` (privileged, `hostPID`, hostPath `/`: writes containerd config and restarts it), Spegel (hostPath containerd socket and content store, privileged init), node-exporter and NFD (standard). All from pinned upstream charts/images; our own privileged piece is the node-config script. | The list must appear in the design doc with the justification; Spegel/Zot are CNCF (sandbox/incubating) and get asked about maintenance. | Keep; document. Move the node-config drop-in entirely into cloud-init (already done for GPU pools) and reduce the DaemonSet to a no-op check, then it needs `hostPath` read-only only. |
| F15 | Low | no | Secrets / UI | The customer UI keeps the API key in `localStorage` (`ui/src/api/client.ts`). | XSS would expose the key; the UI is static nginx with no third-party scripts, so the risk is low. | Accept, or move to a session cookie set by the API later. |
| F16 | Low | no | Networking / DDoS | Internet-facing gateways have a per-key rate limit (3000 req/min, Redis) and Let's Encrypt TLS, no request size cap, no WAF, no anti-DDoS beyond the Nebius load balancer. | Standard question; Nebius LB has no managed DDoS tier to cite. | Add a `ClientTrafficPolicy` with body size and connection limits (Envoy Gateway, no code); document. |

Counts: High 3 (F1, F2, F3), Medium 8 (F4-F11), Low 5 (F12-F16).

Status 2026-10-08 (owner decisions, lane H4):

| Finding | Status |
|---|---|
| F1 | **Accepted** for the managed-service mode (owner 2026-10-08: every model is reviewed before it is offered, tenants submit data, not code; customer data is only handled during the call, inside the Scientific AI layer). Free hardening applied: every rendered container drops all capabilities, `allowPrivilegeEscalation: false`, seccomp `RuntimeDefault`; runner containers non-root uid 10001; job classes may set `runAsNonRoot`; Pod Security Admission `baseline` enforced (`restricted` warn/audit) on tenant namespaces (`charts/tenant`). |
| F2 | **Fixed**: async endpoint calls go through the gateway hostname with the caller's key (`services/api/jobs.py` `build_async`, regional `ENDPOINT_DOMAIN`, runner 0.1.4 `--connect-to` the gateway's in-cluster Service, public TLS name kept; API 0.7.1); `tenant-isolation` no longer allows egress to `models`/`knative-serving`. |
| F3 | **Fixed (light, template scope)**: `.github/workflows/ci.yml` (terraform, helm, kustomize, pytest, trivy on our images, CRITICAL fails), "Patching" in docs/OPERATIONS.md. Semgrep/Renovate deferred (owner: template, keep it light). |

What the review will also note as positives: customer auth is key-based through LiteLLM with budgets, scopes and expiry; the mk8s API endpoints are CIDR-restricted on all three clusters; every cross-cluster credential (MultiKueue kubeconfigs, api-agent kubeconfigs) is short-lived and rotated daily by CronJob; Nebius SA keys rotate weekly with 7-day retirement; tenant RBAC is namespace-bound and the API's ClusterRole is bound per tenant namespace only; a cluster-wide Cilium policy isolates tenant namespaces from everything but DNS, the API server, the endpoints and the world; one bucket per tenant per region with presigned uploads; Kubernetes audit logging to a bucket; versioned Terraform state with a scoped identity; no secrets in git (checked).

## 2. Is a security review needed?

Yes. Per the process a review is required for a client-facing service and for changes to authentication,
authorisation, network exposure and tenant isolation; Serverless 2.0 is all of these (public API and
endpoints, key-based auth, multi-tenant scheduling), and the H2 target makes it a Solutions Library
deliverable that architects deploy into customer tenants. It is non-blocking for development and must be
complete before Preview/GA (external use). Trigger: a YAPF launch ticket or a SECENG "Security review"
ticket filed by the PM/owner with the design doc and a technical contact. The owner's no-contact rule means
this document is the preparation only; filing is the owner's call.

## 3. Review readiness: what the design doc needs and what exists

| Basics / domain | Needed by the review | Exists | Gap |
|---|---|---|---|
| Logical component diagram (user / control / management plane in colours, exposed interfaces) | yes | docs/ARCHITECTURE.md (text, tables), docs/CONTROL-PLANE.md | no diagram; draw one (mermaid) from ARCHITECTURE "v2 topology" + "Components" |
| Sequence diagrams for authn/authz flows | yes | docs/EDGE.md (edge-auth text), docs/API.md | add two: key check at the gateway -> LiteLLM, run submission -> MultiKueue -> worker |
| Deployment diagram (clusters, zones, projects) | optional | docs/FLEET.md, docs/REPRODUCIBILITY.md | text suffices |
| Regulatory scope statement | yes | none | add: none claimed (no SOX/SOC2/HIPAA), data = customer model inputs/outputs |
| Authentication (all external and internal interfaces) | yes | docs/EDGE.md, docs/API.md, docs/CONTROL-PLANE.md (rotation) | table of every interface with its mechanism; operator UIs (F4) |
| Authorisation (who can do what) | yes | docs/OPERATIONS.md "Tenants", charts/tenant, api-agent ClusterRole | one matrix: customer key (normal/admin), operator, ops SA, API identity, MultiKueue, dispatcher |
| Networking (segments, ingress/egress, ACLs) | yes | docs/OPERATIONS.md "Tenant isolation", docs/EDGE.md | add the VPC/NodePort picture (F9) and egress list (NGC, Docker Hub, GHCR, GitHub, Let's Encrypt, Nebius APIs) |
| Encryption in transit / at rest | yes | partial (EDGE.md TLS) | F8, F12 statements |
| Tenant isolation | yes | docs/OPERATIONS.md, docs/SCHEDULING.md | F1, F2, F9, F10 answers; node-sharing statement |
| Logging / Audit Trails | yes | docs/OBSERVABILITY.md | F7 |
| Secret management | yes | docs/OPERATIONS.md "Rotation", docs/BOOTSTRAP.md | F6; inventory table of every Secret with lifetime and rotation |
| User-provided input | yes | docs/JOBS.md (contract) | classification: `image`, `command`, `input_prefix`, endpoint payloads, upload filenames; F10 |
| Sensitive and personal data | yes | none | statement: model inputs/outputs in tenant buckets (customer-classified), keys, no PII by design |
| Availability / backups | yes | docs/OPERATIONS.md (restore drill), VERIFICATION.md | fine |
| Third-party dependencies | yes | docs/ARCHITECTURE.md "Components" | add versions, privileged list (F14), maintenance status, scanner coverage (F3) |
| Vulnerability management | yes | none | F3 |
| Threat list with Avoid/Mitigate/Transfer/Accept | yes | none | derive from this table (F1-F16) |

## 4. What the Terraform-solution target (lane H2) changes in the threat model

- **Operator identity becomes the root of trust.** The architect's IAM identity with `admin` on the
  customer's projects runs `terraform apply`; every generated secret (LiteLLM master key, database
  passwords, Grafana/Argo CD admin, SA keys, tenant keys) is created by that run. The design must say who
  holds that identity after hand-over (customer or Nebius), and prefer `editor` plus explicit permits.
- **The Terraform state bucket holds every secret** unless keys are created with write-only/ephemeral
  attributes or referenced from SecretStash. Treat the bucket as a secret store: one per customer, in the
  customer's project, versioned, access limited to the operators' group, listed in the design doc (F6).
- **One customer per fleet** moves cross-customer isolation to Nebius projects/VPCs (good) and reduces F1,
  F2, F9, F10 to team-level findings inside one customer; the design doc should state the fleet as a
  single-customer trust boundary and list the per-team guarantees (namespace, bucket, Kueue quota, keys).
- **Managed-by-Nebius fleets** need the access path of the operator documented (Nebius identity in the
  customer's project, break-glass, audit of operator actions through the customer's Audit Trails/mk8s audit
  bucket), the patch responsibility (F3) and the data-handling statement (customer data never leaves the
  customer's project; the cache and buckets are theirs).
- **No GitOps controller** removes the GitHub deploy key and Argo CD's cluster-admin Secrets from the picture;
  Terraform's Helm/Kubernetes providers use the operator's kubeconfig instead (short-lived, from the Nebius
  CLI).
- **Public TLS without a domain** (Let's Encrypt IP certificates, H2 item 1) removes the sslip.io shared
  rate limit and the self-signed fallback; the review will still ask for the customer's domain option.
