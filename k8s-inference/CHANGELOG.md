# Changelog

The platform images (`services/api`, `services/dispatcher`, `services/jobs`, `services/ops`; tags in
`stack/config/variables.tf` `images.versions`) and the charts (`charts/fleet`, `charts/platform`, `charts/tenant`;
`version` in each `Chart.yaml`). The current versions are in `README.md` "Versions".

## api 0.11.40

- Continuations of a run on a bounded key (resume, export retry, finalizer, fallback, recovery) are admitted on the
  run's ledger (migration 11 `key_run_members`): one slot and one request per run, members after the current head
  with their own GPU bound, measured settlement, unknown GPU time kept against the budget and reported on
  `GET /v1/keys/me` (`usage`); the 501s of these paths are gone (docs/API.md "Keys").
- A replaced attempt may still close its accounting (interrupted or failed) without promoting a pointer or result;
  an unmeasured GPU attempt is billed as unknown, never as zero; a run cancelled before it started settles at zero.
- A fallback fences the original (suspend, successor mark, Workload deactivated) before its successor is created, and
  restores it when the successor cannot be created.
- API rollouts keep in-flight synchronous calls: surge-only rolling update, a termination grace period (chart value
  `api.terminationGracePeriodSeconds`), a preStop pause and uvicorn's graceful shutdown.

## Initial release

The Kubernetes inference fleet solution: a control cluster and one cluster per GPU region from one
`terraform.tfvars` (two Terraform stages: `cloud`, then `platform` per cluster), one customer API for models,
endpoints that scale to zero, queued and multi-node runs with checkpoint resume and automatic recovery,
per-tenant Kueue queues, a per-region image cache, tenants with API keys and budgets, observability and cost
reporting. Versions at the initial release: api 0.11.39, dispatcher 0.2.12, jobs 0.1.15, ops 0.1.12; charts fleet
0.1.7, platform 0.1.3, tenant 0.3.2.

The pre-merge correctness review is covered by regressions: operation artifact ownership and scopes,
binding revocation permission, key rotation/revocation with durable LiteLLM deletion retries, removed endpoint
variants in retained regions, and record-aware operation lists and replays. Rotation rollback also cancels a
pending deletion of the restored value and serializes it with in-flight mirror retirement.

The API now shares request/response schemas, generates OpenAPI from code, centralizes access policy, and
separates fleet reconciliation from HTTP handlers. `make check` and CI gate Python format/lint (pinned Ruff),
ShellCheck, Terraform formatting/validation, Helm rendering and the API/dispatcher tests.
