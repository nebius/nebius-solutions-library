# Security boundaries and release verification

This is the security design and remaining verification work for the Terraform solution, not a formal security approval. No internal review materials or customer credentials are included.

| Boundary | Implemented control | Release verification |
|---|---|---|
| API / endpoint access | LiteLLM tenant keys; model allow-list checks on catalogue, endpoint reads, invoke and gateway ext-auth; blocked/expired/budget checks | Allowed, forbidden, revoked, expired and exhausted keys through real HTTP and WebSocket routes |
| Gateway identity | Per-endpoint native Envoy SecurityPolicy binds the model in its authorization path; authorization fails closed and reuses the API | Confirm policy attachment and deny traffic with misleading forwarded host headers |
| Model configuration | Admin-only writes, full specs and history; non-admin catalogue responses omit specs | Verify literal configuration cannot leak through user catalogue/history or logs |
| Deployment state | Database transaction commits desired state/history/retry record before Kubernetes writes; serialized reconciliation; optional version guard | Fail one region, restart the API and verify recovery/cleanup |
| Tenant workloads | Namespace RBAC, existing tenant network policies, image allow-lists and workload security contexts | Attempt cross-tenant operation/artifact reads and direct predictor access |
| Secrets | Kubernetes Secret refs and optional shared External Secrets/MysteryBox modules | Verify least-privilege MysteryBox identity and correct namespace/cluster secret distribution |
| TLS | PostgreSQL chain/hostname verification; public ACME or explicit private CA and complete trust bundle | Reject untrusted/mismatched database/gateway certificates; verify certificate renewal |
| Accounting | Idempotent GPU ledger and atomic native LiteLLM increments in one database transaction; shared Redis and strict native proxy checks | Concurrent proxy/job usage, retry after annotation failure and budget reset drills |
| Logs/data | Object Storage-backed Loki; tenant artifact policies; managed database backups | Retention, restore, deletion and access-control tests |
| Availability | API/proxy replicas; dispatcher client leader election and freshness probes | Worker, database, Redis, gateway and dispatcher outage drills against the agreed SLO |

Generated credentials remain in sensitive Terraform state: secure its bucket, access, encryption and recovery. MysteryBox integration does not remove bootstrap credentials from state. Operator interfaces need the customer's access policy (private exposure, CIDR restriction and/or an approved authentication gateway). Redis and single-binary Loki are single replicas by default; durable data and fail-closed behavior do not constitute a zero-downtime promise.

GPU billing occurs after completion and does not reserve a running job's maximum cost. Key-state caching delays revocation by at most the configured API cache interval. Scope keys to models and tenants; rotate bootstrap and application credentials according to the customer's policy. See `RELIABILITY.md` for accounting's pinned-schema contract, upgrade instructions and recovery procedures.

Before customer production use, complete the formal security review and the fresh Managed PostgreSQL, gateway-policy, preemption/resume and backup/failover acceptance tests. Local transaction and manifest tests do not replace those checks.

Public implementation references: [Envoy external authorization](https://gateway.envoyproxy.io/docs/tasks/security/ext-auth/), [LiteLLM production guidance](https://docs.litellm.ai/docs/proxy/prod), [Kubernetes Secrets](https://kubernetes.io/docs/concepts/configuration/secret/), [External Secrets Nebius MysteryBox provider](https://external-secrets.io/latest/provider/nebius-mysterybox/).
