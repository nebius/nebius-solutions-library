# Changelog

The platform images (`services/api`, `services/dispatcher`, `services/jobs`, `services/ops`; tags in
`stack/config/variables.tf` `images.versions`) and the charts (`charts/fleet`, `charts/platform`, `charts/tenant`;
`version` in each `Chart.yaml`). The current versions are in `README.md` "Versions".

## api 0.11.58

- Billing prices a run pinned to its admitting cluster (a resume on the kept volume: region label `north`, a cluster
  id) by resolving the label to the Nebius region the rate snapshot is keyed by (`kube.cluster_region`), as a run
  placed by region already was; no matching key still defers accounting as unknown (never zero). On API54 both
  resumed members of a graph stayed accounting-active ("GPU attempt has no verified pool price") while their original
  siblings settled.

## api 0.11.57

- Bounded retries (docs/JOBS.md "Retries"): a run class's `retry: {max_attempts, on_exit_codes}` and a request's
  `execution.retry` render at most `max_attempts` pods of the same Job, a replacement only after an exit in
  `on_exit_codes` (137/143 by default: the retained scientific policy), any other non-zero exit failing the run at once,
  disruptions still the platform's; the same member, reservation and GPU bound, no second retry owner. `GET /v1/fleet`
  → `execution.retry`. Single-node runs only.

## api 0.11.56

- `timeout_s` of a run and `recovery.deadline_s` accept up to 14 days (7 before): the long-run contract (14-day MD
  runs, registered with 1209600 s) holds for a GPU-budgeted key, which needs the explicit timeout for its bound. No
  quota, limit or default changes; the graph deadline (14 days default, 30 max) is unchanged.

## api 0.11.55

- Delegated stages (docs/JOBS.md "Delegated stages"): a run class carries an operator-owned `delegates` list; a graph
  declares a stage bound to a model (`{"depends_on": [], "model": …}`); the root's owner may then submit that stage on
  a model the key is not granted when the root class delegates to it, the graph is open and the root is active on the
  key (`run:submit` required); nothing else changes, the declaration alone confers nothing. Ledger migration 15.
- Dependency results and delivery metadata name the delivering operation, attempt and epoch (`DEPENDENCY_RESULTS`
  `operation`/`attempt_id`/`epoch` with `producer`; the handle's `exported_by_attempt_id`/`exported_by_epoch`; the
  graph view's `delivered_by`), never a compute attempt next to a finalizer's receipt.

## api 0.11.54, jobs 0.1.20, tenant chart 0.3.3

- The admission an endpoint call's caller presents at the gateway (`x-serverless2-admitted`) is an exact proof of the
  active operation: the token is the HMAC of the key, the model and the operation; the caller names the operation and
  its attempt (`x-serverless2-operation`, `x-serverless2-attempt`, jobs 0.1.20); the hook checks, on every call, that
  the operation holds a live reservation on that key and is not cancelled, and (remembered 30 s) that its Job is that
  key's and that model's, not over, and that the attempt is one of its live pods. A release, a cancel or a terminal
  Job closes the gate at once. A proxied synchronous call is bound to its own `sync-…` admission. The key-only and
  key+model tokens of releases up to 0.11.53 are accepted only with `admitted_legacy = true` (terraform.tfvars), off by
  default. Endpoint SecurityPolicies forward the two headers (re-rendered on the next model apply).
- A waiting stage carries the Kueue queue label naming a queue that never exists (`serverless2-stage-waiting`) instead
  of no label: with a LocalQueue `default` in the tenant namespace Kueue's LocalQueueDefaulting injected `default` into
  the label-less Job and admitted the stage before its dependencies and bindings (API51, op-976ec83a3939e974-s-gpu-0).
  The gate sets the real profile when the stage is admitted; charts/tenant refuses a profile of the waiting queue's name.

## api 0.11.53

- The gateway's authorization hook honours the API's own admitted endpoint call for the scope the admission had: an
  asynchronous-only key (`invoke:async`) reaches its own endpoint call's readiness probe and inference POST; a direct
  call still needs `invoke:sync` and is counted at the hook. The admission token (`x-serverless2-admitted`) is bound
  to the key and the model (`HMAC(router key, <key hash>:<model>)`): a token of one model opens no other, another
  key cannot use it; the key-only form of earlier releases is still accepted for the callers they rendered (removed
  with the next jobs image). (API51 ADMET: readiness GET 403 "key scope invoke:sync required".)

## api 0.11.52

- A public run class (`POST /v1/models`, kind job) takes separate limits and node-local scratch: `cpu_limit`,
  `memory_limit` (the request stays what the scheduler guarantees), `ephemeral_storage`, `ephemeral_storage_limit`
  (docs/JOBS.md "Run class"); absent, nothing changes (memory limit = request, no CPU quota, no ephemeral request).

## api 0.11.51

- An endpoint call's caller carries the fleet's CA bundle (the system roots plus the CAs the API itself trusts, the
  fleet CA of the regional gateways among them: public CA material in the call's own Secret, mounted read-only,
  `CURL_CA_BUNDLE`/`SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`/`AWS_CA_BUNDLE`): a fleet-CA-signed gateway verifies, public
  hosts keep verifying, never an insecure switch; nothing is projected on a fleet with public certificates everywhere.
- An API-managed model's readiness path (`health_path`, published as `endpoints.ready`) survives catalog normalisation:
  the caller probes it (`READY_URL`) instead of the endpoint's root.
- A chain's attempts are each member's own (live pods and records per member): a cancelled parent's handle shows its
  running resumed child RUNNING, as the child's own handle does.
- Weights of an endpoint: `read_only: true` mounts the verified payload readOnly and skips the prepare step (`prepare`
  explicit when needed); defaults unchanged.

## api 0.11.50

- Live sessions (docs/API.md "Sessions"), off unless `sessions.enabled` in terraform.tfvars: `POST /v1/models/<id>:session`
  admits a stream on the key (slot, request, GPU bound) and answers a one-time claim; the gateway hook binds the claim
  exactly once (compare-and-swap, WebSocket subprotocol or `x-serverless2-session-claim` header), injects the session and
  connection ids and refuses client-supplied ones; the worker pod attaches with its verified identity (TokenReview, pod
  UID, the endpoint's own pod) before any GPU work and releases after its cleanup with measured or unknown usage; owner
  cancel, deadline and disconnect are requests; the reconciler ends expired claims, unattached bindings and sessions
  whose pod is gone (unknown), and settles once. Ledger migration 14 (`sessions`); endpoint SecurityPolicies carry the
  session headers (re-rendered on the next model apply).

## api 0.11.49

- A stage of a closed graph (every stage settled) replays as its durable lineage head (200) instead of 409; a stage never
  submitted before the graph closed, or any stage of a cancelled graph, stays 409 and no Job is created.

## api 0.11.48

- Stage lineages, two seams found on 0.11.47 (never deployed): a stage's successor carries the stage root as an
  annotation too, so the second and every later continuation (resume of a resume, export of a resume) keeps the stage
  identity and the bound dependency results; a public `:resume` or `:export` names the current member of its lineage:
  an older member that a newer compute successor continued is refused (409, the head named), never reopened silently
  (an export retry or finalizer of the named member is still followed on the member itself).

## api 0.11.47

- Stage lineages (docs/JOBS.md "Stage composition"; 0.11.46 was never deployed): a stage's `:resume`, `:export`,
  finalizer, fallback and recovery members are admitted on the graph root's key and budget as the next member of that
  stage's own lineage (`keys.stage_head`, `keys.member_of`; the root's own continuations keep their head, stage
  members never become it), siblings continue meanwhile, the successor carries the stage identity and the bound
  dependency results, the gate judges a dependency by the head of its lineage, a FAILED dependency holds its
  dependents (resumable) while a CANCELLED one cancels them once, and a declared stage is submitted once (a
  resubmission answers with its lineage head). Up to 256 stages; graph deadline default 14 days, up to 30.
- An endpoint call's caller pod shares its process namespace between main and the uploader, as a run's pod: the
  attempt's `started` and `finished` records are written from what the uploader sees (upload.sh main_alive); in
  separate namespaces no attempt was ever opened and a broker that grants nothing before the authoritative start
  record held the caller for good.

## api 0.11.46

- Stage composition (docs/JOBS.md "Stage composition"): a root declares its stages at submission (`execution.graph`:
  ids → dependencies, acyclic, at most 64, sealed), each stage is submitted as its own run (`execution.stage`:
  deterministic id `<root>-s-<id>`, same owner, admitted on the root's key as a member of the root's run without the
  head rule); a stage waits as a suspended Job without a Kueue queue (no node, no binding claimed, no credential
  consumed) until the control API's gate finds every dependency `SUCCEEDED` with a `result_ref` on durable records,
  then delivers its binding, binds the dependencies' results into the pod (`DEPENDENCY_RESULTS`) and hands it to
  Kueue; a failed or cancelled dependency, or a success without a result, cancels the stage; the root's `:cancel`
  fans out; the graph's deadline cancels what waits and closes the graph; the root's slot and request are released
  only when every declared stage settled; the root's handle reports `graph` and is terminal only with its stages; a
  stage that settled is never run again (ledger migration 13: `run_graphs`, member `kind`/`stage`/`note`).

## platform chart 0.1.4

- The gateways' access logs use an explicit format: the path without its query string, no credential headers
  (docs/EDGE.md "Access logs"); the gate refuses a proxy that logs the raw path, Authorization, Cookie or an `api_key`.

## api 0.11.45, jobs 0.1.19

- An endpoint call's pod carries the runner's file-system group (fsGroup 10001, as a run's pod): the retained work
  volume is writable and the private binding readable by the non-root caller; the uploader keeps recording under /tmp
  when the volume is not writable. A main container that ended with an exit code the Job's failure policy fails on is
  reported FAILED at once (jobs 0.1.19 for the uploader).

## api 0.11.44

- Deployments keyed by cluster id (`north`) and clients keyed by Nebius region (`eu-north1`) are resolved through the
  fleet document on every path: `:invoke` (sync and async), `GET /v1/models` region status, `GET /v1/endpoints`
  (one entry per deployment with `cluster` and `region`), endpoint detail and scaling; a request's `region` may be
  either; two deployments of one model in one region are never guessed between (400: name the cluster).

## api 0.11.43, jobs 0.1.18

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
- Delivery never turns a failed inference into a success: the caller ends with the captured outcome (0 for 2xx, 2 for
  4xx, 5 for another status) on the first delivery and every re-delivery, and a root closed by a delivering retry stays
  FAILED with the delivery reported next to it (`export_state`, `exported_by`, `result_ref`), live and after the Job is
  gone. Per-operation binding copies carry the binding's uid: a refresh or revocation reaches only the copies of that
  binding (compare-and-swap, annotations kept, deletions with uid and resourceVersion preconditions), a late revoker
  never deletes a binding re-created under the same ref, and the pre-owner continuity checks the record's tenant.
- Attempt rows keep the operation's mode, model and I/O mode (migration 12): an operation whose Job is gone keeps its
  result path and the key's model allow-list.
- Bindings belong to the principal that created them: another principal of the tenant cannot list, replace, revoke
  or consume them (404), continuations inherit the owner, replacement is a compare-and-swap (`If-Match: <revision>`),
  an admin may act on any binding. jobs 0.1.18: `call.sh` with application I/O and transfer-only retries, `upload.sh`
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
0.1.7, platform 0.1.4, tenant 0.3.2.

The pre-merge correctness review is covered by regressions: operation artifact ownership and scopes,
binding revocation permission, key rotation/revocation with durable LiteLLM deletion retries, removed endpoint
variants in retained regions, and record-aware operation lists and replays. Rotation rollback also cancels a
pending deletion of the restored value and serializes it with in-flight mirror retirement.

The API now shares request/response schemas, generates OpenAPI from code, centralizes access policy, and
separates fleet reconciliation from HTTP handlers. `make check` and CI gate Python format/lint (pinned Ruff),
ShellCheck, Terraform formatting/validation, Helm rendering and the API/dispatcher tests.
