# Changelog

The platform images (`services/api`, `services/dispatcher`, `services/jobs`, `services/ops`; tags in
`stack/config/variables.tf` `images.versions`) and the charts (`charts/fleet`, `charts/platform`, `charts/tenant`;
`version` in each `Chart.yaml`). The current versions are in `README.md` "Versions".

## api 0.11.42, jobs 0.1.17

- Endpoint calls with private input and application-owned output (docs/JOBS.md "Endpoint calls with private input and
  application-owned output"): the `execution` block on `mode: async`; the request and the destination from a binding
  (`request.json` or `input_url`; `response_url`, `call_url`, `result_ref`; optional per-attempt `refresh_url` with
  `refresh_token` and the projected identity); the capture (`response.bin`, `call.json`) on a retained work volume,
  delivered by the caller; `EXPORT_FAILED` and a transfer-only `:export` on the retained capture; the gateway hook
  counts the call once (`x-serverless2-admitted`). `GET /v1/fleet` → `execution.async_private_io`.
- Bounded keys are admitted on asynchronous endpoint calls (slot and request; the billing pass releases them).
- The caller probes readiness, posts the inference exactly once (marker before the POST), reports an unknown outcome
  as FAILED instead of posting again; `call.json` timings are RFC 3339 (`first_byte_at`); a delivered 4xx is FAILED
  with `export_state: succeeded`; an endpoint call's export retry is followed on the root (`finalizing_by`); the result
  of an application-I/O call is its `result_ref`, not the platform bucket; a binding's claim is a compare-and-swap, and
  a binding from before owners existed continues under the owner of the operation it is bound to.
- Attempt rows keep the operation's mode, model and I/O mode (migration 12): an operation whose Job is gone keeps its
  result path and the key's model allow-list.
- Bindings belong to the principal that created them: another principal of the tenant cannot list, replace, revoke
  or consume them (404), continuations inherit the owner, replacement is a compare-and-swap (`If-Match: <revision>`),
  an admin may act on any binding. jobs 0.1.17: `call.sh` with application I/O and transfer-only retries, `upload.sh`
  reports the application's export state.

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
