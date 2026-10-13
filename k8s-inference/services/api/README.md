# services/api: the customer API

FastAPI implements [docs/API.md](../../docs/API.md). Its `/openapi.json` contract is generated from
`schemas.py` and the route declarations; `make openapi` exports it without a cluster or database.
The schema tests validate actual handler output against the shared response types and detect undeclared fields.

| Module | Responsibility |
|---|---|
| `app.py` | HTTP routes, invoke admission and operation views |
| `schemas.py` | Request and response types, shared scaling and execution options |
| `auth.py` | Database bearer authentication, admin/scope dependencies, model grants and operation ownership |
| `reconciliation.py` | Desired model generations, apply/remove/variant pruning, idle shutdown, fallback, recovery and reservation/mirror sweeps |
| `models.py`, `render_endpoint.py` | Fleet-dependent definition validation and native endpoint rendering |
| `catalog.py` | Bundled read-only classes and saved model definitions |
| `jobs.py`, `render.py`, `status.py` | Run rendering, submission, continuation and status projection |
| `attempts.py`, `db.py` | Durable, fenced attempt records and model history |
| `execution.py` | Tenant execution profiles, workload identities and confidential input bindings |
| `keys.py`, `billing.py` | Keys, transactional admission, consumption ledger and LiteLLM mirror |
| `kube.py`, `endpoints.py`, `monitoring.py` | Regional Kubernetes clients, endpoint scaling, scoped logs and metrics |
| `artifacts.py`, `resilience.py` | Tenant-bucket I/O and bounded retries |

Authentication consults the fleet database only. LiteLLM holds a best-effort mirror for its OpenAI route.
Scopes are declared on route dependencies; invokes and the gateway hook share model authorization.
Operation reads, results, logs, metrics and operation-object downloads enforce the same tenant, owner and
model grants. Binding writes and revocation require `artifacts:write`; downloads require `read:results`.
The router key is accepted only at the model gateway.

Model definitions commit desired state and history before reconciliation. The controller holds the database
reconciler lock and completes only the matching generation. Partial apply/delete failures remain pending.
Removed variants are pruned in retained regions, including their LiteLLM deployments; gateway authorization
is retained until the corresponding predictor routes disappear. Key revocations and rotations also persist
pending mirror deletion, with retries while LiteLLM is unavailable.

Model options retain their raw input until `models.validate` checks them: Pydantic must not coerce an invalid
ServiceAccount-token flag or drop an unknown pod-metadata field before that check. Shared scaling, ephemeral
volume and execution types are used by their validators as well as the generated contract. Response schemas
document and test the wire format without adding defaults or filtering the handler's output.

The fleet manager creates runs on MultiKueue; Kueue and the dispatcher place them in eligible GPU regions.
A single-cluster API creates local Jobs. The runner image (`services/jobs`) fetches inputs and records/delivers
outputs. Durable attempt records preserve terminal delivery status after worker pods disappear and keep
operation lists and idempotent replays consistent with single-operation reads.

Build the API with `docker build -f services/api/Dockerfile .` from `k8s-inference`.
Terraform supplies the image tags, tenant Secrets and `python -m keys bootstrap`; the API applies database
migrations at startup. The billing CronJob uses the same image. See [docs/OPERATIONS.md](../../docs/OPERATIONS.md)
for deployment and acceptance checks.

Development: `make venv`, install ShellCheck, then `make check`. `make format` applies Python/import and
Terraform formatting; `make lint` checks Ruff, ShellCheck and Terraform formatting. Unit tests fake Kubernetes
and LiteLLM; `TEST_DATABASE_URL` enables isolated real-PostgreSQL transaction tests (CI provides PostgreSQL).
