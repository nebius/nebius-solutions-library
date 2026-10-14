# Changelog

The platform images (`services/api`, `services/dispatcher`, `services/jobs`, `services/ops`; tags in
`stack/config/variables.tf` `images.versions`) and the charts (`charts/fleet`, `charts/platform`, `charts/tenant`;
`version` in each `Chart.yaml`). The current versions are in `README.md` "Versions".

## api 0.11.67 (hotfix on 0.11.63; supersedes the unapplied 0.11.65; 0.11.64 is the separate isolation candidate)

- Manual `:resume` links its successor (docs/JOBS.md "Resume"): the resumed operation gets the durable successor mark
  and, while its Job exists, the `successor` annotation, as fallback and recovery always did. Until now a public resume
  of a FAILED run left the original handle on its own terminal state for ever once its Job expired (TTL), and a graph
  child waiting on that root kept seeing the FAILED original; `GET /v1/operations/<original>` now follows to the new head
  and the stage gate waits on the successor's state: the handle of a resumed operation is its lineage head, as it
  already was for fallback and recovery (`resumed_from` and `root_operation_id` still name the lineage). `:resume`,
  `:export` and `:cancel` act on the operation named (a resume or export of an older member is refused naming the
  head, as before). The run ledger's lineage head is the authority behind the link: a handle follows it even when no
  annotation or mark exists (a continuation admitted on 0.11.63), also when the head's own Job expired (its durable
  attempt rows tell a compute continuation from an export retry or finalizer), with the owner check on the named
  operation and on the head alike. `:cancel` by a graph root's id cancels the graph also once the root's Job expired (its stages and
  the root lineage's head, a resumed root still queued or running, are cancelled and the graph is closed); an expired
  operation that is no graph root is over (409). No other change against 0.11.63.
## api 0.11.64, platform chart 0.1.7

- Ingress isolation (docs/API.md "Ingress isolation"): `ingress_isolation: declared-ports` on an endpoint model renders a
  namespaced `CiliumNetworkPolicy <id>-ingress` on that endpoint's pods admitting only the platform's data path (the
  gateway and the Knative activator to the queue-proxy, or to the declared port of a multi-node leader), the node,
  Prometheus and the replica group's own pods; a second port an engine binds on 0.0.0.0, and the container port itself,
  are reachable from no other model, tenant or namespace. Opt-in: nothing changes for a model that does not name it;
  egress is not touched. The platform chart gives both API Roles `ciliumnetworkpolicies` in `models` (check.sh invariant).
- Native execution leases: a worker container that restarted inside the same pod (same UID) is a confirmed loss: the
  restart count of the non-queue-proxy containers is recorded at attach (migration 18) and compared by the sweep;
  `lost_reason` on the handle says why an execution was lost (pod gone | pod replaced | pod finished | worker container
  restarted). A queue-proxy restart, readiness, a timeout or an unreadable API still never count.

## jobs 0.1.22

- Caller readiness (services/jobs/bin/call.sh): the gateway holds a readiness GET while a cold replica starts, so a probe
  that ends without an answer only means "not yet"; the probes are now short (`READY_TIMEOUT` 30 s, was 120 s) with
  pauses of 2 s doubling to `READY_BACKOFF_MAX` 10 s (was 5 s doubling to 320 s), so a ready replica is seen within about
  40 s instead of minutes; bounded by `READY_MAX_WAIT` (default the call's timeout) of wall-clock time, past which the
  call fails explicitly (exit 5) instead of running into the Job's deadline. The inference POST keeps its own backoff and
  retry budget and is still made exactly once.

## api 0.11.63, jobs 0.1.21

- Work mount aliases (docs/JOBS.md "Work mount aliases"): `job.workMountAliases` / spec `work_mount_aliases`
  `[{path, scope: attempt | run}]` mounts the run's own work volume at a canonical absolute path on `main` (and so on the
  CPU finalizer, which sees the finalized attempt's directory); `scope: attempt` is a per-attempt subdirectory from the
  pod's own name (`$(S2_POD_NAME)`, platform-owned, no caller interpolation), so retries and resumes never share a
  canonical writable output. Reserved and overlapping paths refused, single-node runs only, no new storage, no host path,
  no symlink. The fetch init container (jobs 0.1.21) creates the directories `2770` for the pod's fsGroup (tenant-private).
- Attempt image digest, observed and convergent (docs/JOBS.md "Attempts and finalization"): the uploader (jobs 0.1.21)
  waits, bounded, for the kubelet's `imageID` of main before its start record and republishes the start record once when
  the digest appears later; the API fills a start record from the pod it reads and, at GET time, from the newest
  attempt's live pod. `image_digest_state: observed | pending | unknown` on the operation says what a null means
  (`pending` is transient). Never the expected image.
- Native execution leases: a row an earlier caller only prepared (never attached) whose pod is gone or finished (exact
  read) is closed as `caller replaced` when the replacement pod posts, so a preempted async caller's replacement is not
  refused for the rest of the call; an attached row is never freed this way.

## api 0.11.62

- Durable terminal fence for native executions (docs/API.md "Native execution leases"): the join writes
  `native_fences` in the transaction that decides the settlement, and the fence outlives the ledger release; prepare and
  attach for any attempt of a fenced operation (seen before or never) are 409. Closes the gap in which a prepare of an
  absent or new attempt could slip in between the join closing the prepared rows and the reservation release. Ledger
  migration 17.
- Lease clients (docs/API.md "Lease clients"): a model with `native_lease` gets a projected pod-bound ServiceAccount
  token (audience `serverless2`, 1 h, kubelet-renewed) at `/var/run/secrets/serverless2/token` and the environment
  `S2_API_URL`, `S2_LEASE_TOKEN_FILE`, `S2_LEASE_MODE`, `S2_LEASE_AUDIENCE` (and `S2_CA_PEM` on a fleet whose control
  hostnames are not publicly signed), on both the Knative and the multi-node shapes; the apply shape is unchanged.
  `sessions.token_audience` defaults to `serverless2` (the audience the projection uses).
  `services/lease-client/s2lease.py`: the shared standard-library worker client (attach before GPU enqueue, fail
  closed, same-tuple retries on transport errors only, cancel poll, release after settlement, owner context).

## api 0.11.61, platform chart 0.1.6

- Native execution leases (docs/API.md "Native execution leases"): an endpoint call's reservation outlives its CPU
  caller and a cancel until the worker settled or its loss is confirmed. The hook prepares one native execution per
  operation on the verified inference POST (a retry attempt's POST is refused while another attempt's execution is
  prepared or attached); the worker attaches atomically before GPU enqueue (`POST /internal/executions/<op>:attach`,
  pod-authenticated; stale attempt, other pod or no prepared execution: 409, fail closed), reads
  `cancel_requested`, and releases after its own settlement with the wall-clock lane occupancy and the GPU seconds it
  measured (null = unknown, never zero). The member settles at the JOIN of the caller's end and the execution's end
  (billing), never first-one-wins; a lost worker (exact pod UID gone or terminated, nothing else) settles unknown.
  `native_lease: optional | required` per endpoint model (`required`: a caller that ended unattached settles unknown).
  Every attempt keeps its own row (evidence summed at the join, unknown stays unknown); attach is fenced atomically
  against a closed row (the join closes a never-attached execution when the caller is over), a cancel before attach, a
  released reservation, a stale attempt or another pod; release is exact to (attempt, pod, request id). The lease
  routes answer with the operation's durable owner context (`owner: {tenant, owner, model}`): the trusted owner group
  for a worker, never a client header.
  The policy forwards the verified `x-serverless2-operation` / `x-serverless2-attempt` to the backend (empty on a
  direct call; a client's values are overridden). Ledger migration 16 (`native_executions`). Handle `native`.
- Rollout state: all three revision names must be present and equal for `converged` (an absent
  `latestRolledoutRevision` is "not converged"), as the next revision after the frozen 0.11.60.

## fleet chart 0.1.8

- Every ClusterQueue covers `ephemeral-storage` (the kubelet's node-local scratch a run class may request since api
  0.11.52, `ephemeral_storage`) with a budget per flavor from the pool's boot disk: per node `boot_disk_gib x
  node_reserve.ephemeral_fraction (0.85) - node_reserve.ephemeral_gib (4)`, times the pool's nodes, lent and borrowed
  like CPU and memory (the system flavor from the system nodes' fixed 128 GiB disk). Under what the kubelet reports
  allocatable (a 128 GiB disk reports ~109.6 Gi, a 512 GiB one ~444.4 Gi; the budget is 104 / 431 Gi per node): a run
  requesting scratch was `NoMatchingFlavor: resource ephemeral-storage unavailable in ClusterQueue`. Nothing is
  provisioned or changed in the cloud; `tools/check.sh` asserts the coverage and a budget on every flavor.
  A literal `ephemeral_gib = 0` or `ephemeral_fraction` is honoured as given (not treated as unset).

## platform chart 0.1.5

- The API reads Knative Revisions in the models namespace (`serving.knative.dev` revisions get/list) on every cluster:
  the control API's own Role (`serverless2-api-endpoints`) and the regional agent's Role (`serverless2-api-agent-models`,
  what the control API reaches a GPU region with). The latest ready Revision's stored image is the convergence proof of
  an endpoint (api 0.11.60); without the rule the rollout state reports "not converged" with the Revision unknown.
  `tools/check.sh` asserts both rendered Roles carry the read-only rule.

## api 0.11.60

- Rollout convergence is observed, never inferred from revision names (0.11.59 was never deployed): the
  InferenceService's spec generation must be reconciled (`observed_generation`), the created, ready and rolled-out
  revisions one, the latest ready Revision object's stored image equal to the spec's desired image (valid for a
  scaled-to-zero immutable revision, no pod needed), and every ready pod on that image (by digest or reference);
  `regions[].rollout` carries the evidence and a `reason` when not converged, which the convergence fence (409) names.

## api 0.11.59

- Run classes carry a sanitized `execution` identity on the public model (`image`, `immutable`, `images`,
  `gpu_classes`, `nodes`, `delegates`, `retry`, `finalize`; never the container configuration) and a non-null
  top-level `image` (docs/API.md "Execution identity and rollout state"): a key granted a class discovers it
  without `spec` (restricted keys saw `image: null`, `spec: null`).
- Endpoints report their rollout state (`regions[].rollout`: created, ready and rolled-out revisions, serving images,
  `converged`); an endpoint call may require convergence (`execution.require_converged`: 409 while diverged, admitted
  at zero when converged) and records the ready revision and image at admission (`endpoint_revision`,
  `endpoint_image`), labelled as admission-time evidence.

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
