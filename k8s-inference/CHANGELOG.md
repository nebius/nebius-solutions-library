# Changelog

The platform images (`services/api`, `services/dispatcher`, `services/jobs`, `services/ops`; tags in
`stack/config/variables.tf` `images.versions`) and the charts (`charts/fleet`, `charts/platform`, `charts/tenant`;
`version` in each `Chart.yaml`). The current versions are in `README.md` "Versions".

## api 0.11.110

(0.11.110 = 0.11.109 plus the fleet operator surface, the provisioning of a gang's shared checkpoint leaf and the
member-aware reservation fence of the worker's attach; jobs 0.1.27 and platform chart 0.1.12 unchanged; no migration.)

- Fleet operators (docs/API.md "Fleet operators"): a key with `metadata.role=fleet` (tenant `platform`, minted only by
  the fleet's own bootstrap, `python -m keys bootstrap` with role `fleet`; never by a tenant admin, never by an
  import) gets the explicit cross-tenant surface `GET /v1/fleet/tenants[/{tenant}]` (the fleet document's tenants and
  every tenant with keys on record: quota, fair share, dedicated pools, assets, key counts, spend, what the keys hold)
  and `/v1/fleet/tenants/{tenant}/keys` (list, create, `:import`, PATCH budget/models/expiry, `:block` and unblock,
  DELETE) through the tenant's own key functions with the same safe projection. Tenant admins stay tenant-scoped (403
  on the fleet surface); the fleet operator is no tenant admin (403 on `/v1/keys`) and runs nothing. Same database,
  same auth: no new identity store.

- Multi-node runs (docs/JOBS.md "Multi-node runs"): with `checkpoints: shared` every rank pod (and the gang's CPU
  finalizer) runs the trusted scratch provisioning init container on exactly the gang's checkpoint leaf
  (`checkpoints/<root>` on the tenant's shared claim, the same mount) before main and the uploader: the kubelet
  creates a missing subPath directory root-owned 0755 and the ranks (10001, no capability) could not write it (the
  first real 2x1 MPI run on a fresh root, 2026-10-14: `/work/checkpoint` not writable while `/work` was). chown
  10001:10001 and chmod 2770 of the mount point only, not recursive, CHOWN/FOWNER/DAC_OVERRIDE only, idempotent on
  a restart or a resume; a local-checkpoint gang is untouched. A `:resume` rewrites the leaf to the ROOT's
  (`checkpoints/<root>`) on every container of the rank pod, the provisioning init included (0c61979f9 rewrote the
  main/uploader mounts only, so a same-root resume would have provisioned a fresh `checkpoints/<successor>`).

- Native execution leases (docs/API.md "Native execution leases"): the worker's attach checks the operation's LIVE
  reservation through the durable run ledger (`keys.reservation_key_of`: its own reservation, or its root's for an
  ACTIVE member: a continuation, a stage, an endpoint child); before, it looked for the operation's own reservation
  row, which an endpoint child never has (it shares its root's slot), so the first LeRobot child's inference POST on
  109 reached the hook, was prepared, and the worker's attach was refused "reservation is released" with the root
  live (op-82b6134819bee769). The gateway's admission proof already resolved members this way; both now share the one
  resolver. A settled member never borrows its root's live slot; a released root resolves to nothing for itself and
  its members. Tests: root, child, foreign, settled child, released root, the child's attach.

## api 0.11.109

(0.11.109 = 0.11.108 plus the restore triple on every attempt entry and the Knative Service as the rollout witness; jobs
0.1.27 and platform chart 0.1.12 unchanged; no migration.)

- Gang attempts (docs/JOBS.md "Restoring a gang"): every `attempts[]` entry of a gang record (an observed rank, where
  the start ACK froze the pointer) carries `resume_checkpoint_ref`, `resume_source_epoch` and `resume_source`, all
  three null when the ACK decided there is nothing to restore (a fresh start); before, null keys were omitted, so a
  fresh gang's peers showed no triple while the current attempt did, and a restore that reads every peer's triple
  refused it (op-a229254b4a059926, 2026-10-14). Rows the ACK never decided (a single-pod attempt, a live pod without
  its record) carry no such keys: absent is not fresh. A resumed incarnation shows the identical frozen triple on
  every peer; earlier incarnations keep their own.
- Endpoints (docs/API.md "Execution identity and rollout state"): the rollout's reconciliation witness is the predictor
  Knative Service (its `generation == observedGeneration` and its template image equal to the InferenceService's
  desired image), reported as `service_generation`, `service_observed_generation`, `service_image`; KServe 0.20 writes
  the Service's observedGeneration onto the InferenceService, so a never edited ISVC showed `generation 1 / observed
  2` for good and every ready Knative app read as "not reconciled" in the admin display (SAM2 proof, 2026-10-14).
  In proven RawDeployment mode (the ISVC's own `deploymentMode`) the ISVC generations decide as before; in Knative
  mode an unreadable predictor Service is "not converged" with the reason, never a fallback. Nothing weakened: the
  revision identity, the ready Revision's image and the serving pods' images are still all required.

## api 0.11.108

(0.11.108 = 0.11.107 plus the whole-history rule of the unit retry; 0.11.107 was built but never applied. jobs 0.1.27 and
platform chart 0.1.12 unchanged; no new migration.)

- Endpoint children (unit retry, `can_retry`): the "never inferred" verdict reads EVERY attempt record of the child's
  lineage (its transfer-only `-x<n>` retries included), not the last one: one exit 3, exit 4, an attempt without an
  exit on record, or any capture anywhere in the history makes the unit "maybe inferred" (no retry), so a later exit 2
  (an export retry refused at its input refresh) never erases an earlier unknown outcome. Regressions for the mixed
  histories.

## api 0.11.107

(0.11.107 = 0.11.106 plus the unit retry of endpoint children and the settlement lock's database import; jobs 0.1.27 and
platform chart 0.1.12 unchanged; migration 28.)

- Endpoint children (docs/JOBS.md "Endpoint children", unit retry): a unit whose child ended WITHOUT a delivered
  capture (the cascade of a parent that finished without a successor, a failed or refused caller) is reopened by a
  live attempt of a LATER parent of the same root (the owner's resume) asking for the same bound request with the
  explicit `retry: true`, and only when the child definitely never inferred (no attempt, or exit 2/3 with no capture;
  an outcome-unknown exit 4 is refused): the next incarnation
  `<root>-c-<sha>-r<n>` (a new Job, binding and ledger member of kind `child` for the same unit; the earlier child's
  Job and records stay as history), the row back to `pending`/`active` with the new parent, `incarnation` and
  `previous_child` in the readback and the owner route. Before, the same request replayed the ended row (200, no
  capture) and the unit could never run again under that root: the first LeRobot resume chain (2026-10-14) failed
  its stage for exactly that. Unchanged: an ended unit WITH a capture is final (replay, never a second inference), a
  different request is 409, one open child per root, `child_max_units` counts units. Migration 28:
  `endpoint_children.incarnation`, `previous_child`.
- Endpoint children: the readback (owner route and the parent's internal read) carries `can_retry`: true only for an
  ended unit with no capture whose child definitely never inferred (no attempt, or exit 2 with no capture manifest);
  exit 3/4 and every attempt with a capture say false. The explicit POST `retry: true` applies the same predicate
  server-side (409 otherwise).
- Billing: a cancelled JobSet (suspended, pods deleted, a manager's worker copy removed by MultiKueue) never reaches
  Complete/Failed, so neither the pass nor the owner's read settled it and its key stayed reserved (the first MPI
  cancel, 2026-10-14: `active_operations 1`, `usage pending`). It now settles once its cancel is on record, it is
  suspended, no pod of it is left and Kueue holds nothing of it (`kube.workload_inactive`: Workload gone, deactivated
  or finished), measured from the rank records like a finished run (unknown when a rank has no end); never before.
- Billing: the settlement lock (0.11.106) reads the database URL from `db`, not `config` (the import failed live and
  every settlement ran on the idempotent unlocked path, correctly but without the duplicate-work guard).

## api 0.11.106

(0.11.106 = 0.11.105 plus the session attach's owner context and the settlement lock on its own connection; jobs 0.1.27
and platform chart 0.1.12 unchanged; no migration.)

- Sessions (docs/API.md "Sessions"): `POST /internal/sessions/{id}:attach` and `GET /internal/sessions/{id}` answer the
  attached worker with `owner: {tenant, owner, model}` from the session's durable row (the admitting key's principal
  resolved at creation; never a request header, never a group derived from the session id, never the key), as the
  native execution attach does: a worker keys its own owner-fair state on it. Stable on a duplicate attach; the
  exact-model and pod checks of the worker's identity are unchanged; nothing new in the owner-facing session reads.
- Billing: the per-operation settlement lock (0.11.105, `billing._SettlementLock`) holds a dedicated short-lived
  connection, never a pool slot: the settlement behind it takes pool connections of its own (ledger, attempt records,
  charge), so with four concurrent settlements on 0.11.105 the pool (max 4) could starve and the readers stall 30 s
  until the pool timeout (settled by the CronJob later; no wrong charge). Found through the test suite, where a fake
  rig with no database behind the pool made the same wait visible as a stall.

## api 0.11.105

(0.11.105 = 0.11.104 plus settlement on the owner's read, delegated child readiness and the JobSet rule name; jobs 0.1.27
and platform chart 0.1.12 unchanged; no migration.)

- Billing (docs/JOBS.md "Billing"): a finished operation its owner reads (`GET /v1/operations/{id}`) before the billing
  CronJob reached it is settled by that read: the pass's own body for one operation (`billing.settle_job`, lifted out
  of `bill_once`, which keeps calling it) under a per-operation Postgres advisory lock, so the same answer already
  carries `usage: measured`, `gpu_seconds`, `cost`, and the key's slot is free, instead of `pending` and a held slot
  until the next pass (every 2 minutes: the first real MPI run was qualified against `pending` 74 s before its pass).
  Same ledger, same semantics, once: the `charges` receipt is unique per operation, the release conditional, the
  annotation re-read under the lock; concurrent readers and the CronJob settle exactly once; a reader meeting the lock
  held by another settler answers the view before the settlement once. Unchanged: an unfinished operation, a foreign
  reader (404), an unmeasured attempt (`unknown`, the reservation kept, never zero), a native call whose worker is
  still attached (deferred), and the CronJob as the settler of record for operations nobody reads.
- Endpoint children (docs/JOBS.md "Endpoint children"): the child's caller, with its operation-scoped credential,
  passes the gateway hook on a parent-only grant (a key allowed the run class but not the child endpoint): the hook's
  proof already binds the credential to exactly that child, its model and a live attempt, and the child model was
  allowed by the run class's frozen `child_endpoints` at admission, so the key's own model allow-list is not applied
  to a live child (`keys.member_of` kind `child`, active); scope (`invoke:sync` or `invoke:async`) and budget checks
  stay, the key itself still cannot call the child endpoint directly, and the credential is still refused for another
  model, unit or attempt, a cancelled child and a settled one. Before, readiness answered 403 "key is not allowed to
  use model <child>" and the parent failed ChildUnresolved (the first LeRobot child on a QA grant, 2026-10-14).
- Multi-node runs (docs/JOBS.md "Delivery pending never restarts the gang"): the JobSet failure-policy rule is named
  `delivery_pending`; the JobSet validating webhook accepts rule names of `[A-Za-z0-9_:]+` only, so 0.11.104's
  `delivery-pending` had every gang with a finalizer refused 422 at creation (ADMISSION_PENDING, no JobSet: the first
  MPI 2x1 run, 2026-10-14). The protection itself (FailJob on the delivery exit, FailJobSet on that reason) is
  unchanged; the regression checks every emitted rule name against the webhook's rule.

## api 0.11.104

(0.11.104 = 0.11.103 plus idempotent session creation and the gang finalizer's image with per-class images; jobs 0.1.27
and platform chart 0.1.12 unchanged; migration 27.)

- Sessions (docs/API.md "Sessions", "Idempotent creation"): `POST /v1/models/{model}:session` takes an owner-scoped
  `Idempotency-Key` (1..200 chars) over the semantic request (effective model, deployment, timeout_s): the same request
  again answers the same session (`replay: true`), one row, one admission; a concurrent duplicate loses the unique
  index, releases its own admission and answers the winner's. While the session is still `claimed` (unexpired, not
  cancelled) the replay carries a usable claim that replaces the earlier token (still exactly one connection ever
  binds); once bound, attached or ended the replay is the same id without a claim; nothing is extended; the key with
  another request is 409. A replay keeps the key's model grant, scope and validity checks but takes no new request
  from the budget (the admission counted once: a key whose last request the session was recovers its lost answer,
  never 402). Every create answers `replay`. Migration 27: `sessions.idempotency_key`, `request_hash`,
  the unique index `(tenant, owner, idempotency_key)`. Tests: lost answer / repeat, bind once / replay denied,
  conflict, peer 404, concurrent duplicates, expired and cancelled claims.
- Keys (docs/API.md "Keys"): the admin listing `GET /v1/keys` (and the create/update answers) carries the same safe
  limit and usage fields as the caller's own `GET /v1/keys/me` (`scopes`, `max_concurrent`, `budget_requests` /
  `spent_requests`, `budget_gpu_seconds` / `spent_gpu_seconds`, `rate_limit`, `principal_id`, `usage`), plus
  `blocked`; a retained blocked key (imported with unknown scopes, or blocked) now says `status: blocked` instead of
  `active`; `exhausted` covers the request and GPU-second budgets as `/me` does. Never the hash, the raw metadata, the
  legacy blob or a secret (`id` stays the alias). Migration admin parity for the console, no new feature.
- Gang finalization (docs/JOBS.md "Restoring a gang"): the one-CPU finalizer of a gang whose class carries per-class
  images (`job.images {default, <class>}`, the shape that fixes a run's image identity) renders with the image of the
  class the gang was admitted on (the `gpu-class` label), `job.images` dropped with the other compute-only keys; the
  finalized attempt's recorded digest still wins. Before, the CPU render cleared `gpu_classes` but kept `job.images`
  and the finalizer was refused (500 "job.images names classes ... not in gpu.classes []"): no delivery of the gang's
  publication at all (the first real MPI run, 2026-10-14). Proposed and tested by the MPI agent
  (S2-CPU-FINALIZER-CLASS-MAP); regression on the gang finalizer with and without a recorded digest.

## api 0.11.103

(0.11.103 = 0.11.102 plus the child binding's cluster and the log shipper on CPU pools; jobs 0.1.27 and platform chart
0.1.12 unchanged; no migration. `clusters/common/values/alloy.yaml` changes: the Alloy DaemonSet rolls once per cluster.)

- Endpoint children (docs/JOBS.md "Endpoint children"): the child's private binding is written on the API's own cluster,
  where every delivery reads bindings (a caller's `PUT /v1/bindings/{ref}` lands there too), and `jobs.create` names
  that cluster explicitly; the `<child>-private` copy still lands in the child's region with its Job. Before, a
  control-cluster API wrote the binding in the child's region and the delivery looked on the control cluster: every
  child of a fleet-placed run was refused 404 "no such binding", the intent rolled back and an orphan `binding-<child>`
  Secret stayed in the region (the first live LeRobot child, 2026-10-14). Regression: a control API, the parent and the
  child endpoint in a region; the binding on control, claimed and owned; the copy and the Job in the region; nothing
  orphaned; the replay reads the same child back.
- Logs (docs/OBSERVABILITY.md): the Alloy log shipper tolerates the CPU-pool taint (`serverless2.nebius/cpu-pool`), so
  runs on CPU pools ship their logs; before, no shipper ran on those nodes and `GET /v1/operations/{id}/logs` was empty
  for every CPU-pool run (the pod's files go with the worker Job). `tools/check.sh` asserts the shipper tolerates every
  worker-pool taint the cluster module places.

## api 0.11.102

(0.11.102 = 0.11.101 plus operator-declared parameter bounds; jobs 0.1.27 and platform chart unchanged; no migration.)

- Run classes (docs/JOBS.md "Parameters"): `parameters[].min`/`max`/`integer` for `number`, enforced `options` for
  `select`, and `job.maxGpus` (nodes x GPUs per node) are checked at invoke on the effective values, before any
  reservation or Job creation (400 naming the parameter and the bound): an operator catalog class with a templated
  shape admits only operator-approved shapes under one model id, whatever key calls it directly (the app's own
  validator was the only bound before). Malformed numerics (strings, booleans, NaN/inf, fractions where an integer is
  declared) are refused, never coerced. Classes declaring no bounds are untouched (the bundled classes included).

## api 0.11.101

(0.11.101 = 0.11.100 plus the gang resume pointer; jobs 0.1.27 and platform chart 0.1.11 unchanged; migration 26.)

- Gang attempts (docs/JOBS.md "Restoring a gang"): the start ACK of every rank carries `resume_checkpoint_ref`,
  `resume_source_epoch` and `resume_source` (`committed` | `explicit`), chosen once when the incarnation opens from the
  newest committed coordinator pointer of an EARLIER epoch of the same root, else the explicit continuation reference
  the run was rendered with; frozen on the incarnation's first row and copied to every sibling, so all ranks restore the
  same state and no rank ever reads a same-epoch or peer pointer; the owner-scoped operation exposes the same three
  fields on the current attempt and every `attempts[]` entry. Ordinary single-pod runs carry none. No application
  pointer or ledger; the uploader's ACK handling is unchanged (the ACK already lands in `/work/attempts/.ack.json`).

## api 0.11.100, jobs 0.1.27

(0.11.100 = 0.11.99 plus application delivery for endpoint children; platform chart 0.1.11 unchanged; migration 25.)

- Endpoint children on a tenant with external storage (docs/JOBS.md "Endpoint children", "Application delivery"): a
  class declares `child_delivery: application`; the PARENT's admission preflights the capture backend (platform mode on
  an external tenant: 400 before admission, naming the region/model/fix; application mode: no storage probe at all;
  before: the child POST answered 404 after admission); each child request carries the application's delivery
  (`transport.refresh_url` + `refresh_token`, the existing call.sh refresh contract, or static URLs) and an immutable
  `result_ref` bound into the request; the child's caller inherits the parent's frozen execution (identity, profile)
  so the broker receives the child's own projected identity token; no customer key or storage credential enters the
  child; the capture is recorded durably by the platform's uploader (`attempts.capture`: sha256, media type, size,
  status, the ORIGINAL producing attempt, timings; allowlisted, immutable per lineage, 409 on contradiction) and the
  readback takes it from there with no download URL; the platform broker refuses such a child (409); replay with
  renewed transport rewrites only the binding's URL/token keys. `execution.endpoint_children` capability block.
- jobs 0.1.27: call.sh's RESULT.json carries the capture manifest (`capture` object) it just made durable; upload.sh
  forwards it with the attempt record. Nothing else of the one-POST protocol changed.

## api 0.11.99

(0.11.99 = 0.11.98 plus the observer fields; jobs 0.1.26 and platform chart 0.1.11 unchanged; no migration.)

- The owner-scoped operation exposes the observed durable fields of its current attempt (`pod_uid`, `gang_rank`,
  `gang_restart`, from the authority record) and on every `attempts[]` entry (from the record; from the live pod's own
  UID and JobSet labels while it exists; the durable view after the Job's TTL too), so an application broker can verify
  a gang peer against the observed coordinator of the same epoch and incarnation without trusting a caller's claim.
  No new endpoint, no ledger; owner access unchanged.

## api 0.11.98

(0.11.98 = 0.11.97 plus one narrow correction; jobs 0.1.26 and platform chart 0.1.11 unchanged; no migration.)

- Attempt records: `pod_uid` (migration 24) is now the observed UID of EVERY recording pod (a single-node run, an
  endpoint caller, a CPU finalizer, a gang rank), taken from the one pod read the API already does for the runtime
  digest (`attempts._pod_facts`), never from the record or the name; a later record of a pod that cannot be read
  any more keeps the UID and digest its row holds. 97 persisted the UID for gang pods only. The unverified-gang
  refusal and the old-row identity rule are unchanged; ordinary unreadable pods record as before.

## api 0.11.97

(0.11.97 = 0.11.96 with one fence corrected after review; jobs 0.1.26 and platform chart 0.1.11 unchanged. 0.11.96 was
never applied.)

- Gang attempts: a gang pod the API cannot read and never recorded is refused (409 "membership unverifiable"), never
  attributed to the newest gang by its missing rank (96 joined it: an old incarnation's coordinator coming back late
  could have acquired the current epoch's publication authority); a known pod keeps its own epoch and fences.
  Regression: incarnation 2's peer recorded, its coordinator not yet; incarnation 1's deleted coordinator sends late
  records: stale for a start/success, closes on its own interruption promoting nothing, never epoch 2; an unrecorded
  unreadable rank-0 claimant refused; incarnation 2's rank 0 then holds the authority.

## api 0.11.96 (unreleased candidate, superseded by 0.11.97)

(0.11.96 = 0.11.95 plus gang attempts for multi-node runs; jobs 0.1.26 and platform chart 0.1.11 unchanged.)

- Gang attempts (migration 24; docs/JOBS.md "Gang attempts"): the pods of one JobSet incarnation share one epoch (the
  rank and incarnation observed on the pod by the API: `batch.kubernetes.io/job-completion-index`,
  `jobset.sigs.k8s.io/restart-attempt`, the pod UID; never caller-declared), so siblings no longer fence each other
  (before: every sibling took the next epoch and the coordinator's checkpoint/success answered 409 "stale record
  refused"); a prior incarnation stays fenced; only rank 0 publishes `checkpoint_ref`/`result_ref` (peers' dropped,
  their accounting kept); the operation's `attempt_id`/`epoch` name the coordinator of the newest incarnation and the
  gang's outcome counts every rank. Ordinary single-pod runs unchanged.
- "Delivery pending" on a gang: a class with `finalize.on_exit_codes` now installs the `podFailurePolicy` on the
  indexed Job AND a JobSet failure-policy rule (`FailJobSet` on reason `PodFailurePolicy`), so exit 75 ends the gang
  instead of a whole-gang `Recreate` recompute (JobSet ≥ v0.6).
- `:export` of a completed multi-node run (`checkpoints: shared`, application I/O): the model's finalizer as ONE CPU
  pod on the shared checkpoint subPath, the coordinator's record as the finalized attempt (`FINALIZE_RANK`,
  `FINALIZE_GANG_RESTART`, `FINALIZE_NODES`, `CHECKPOINT_DIR`), never the gang again; a gang without shared checkpoints
  or without a finalizer is refused explicitly (409 naming why) instead of "no kept work volume".
- RDMA memlock stated, not granted: an InfiniBand gang's main gets `S2_RDMA_MEMLOCK_POLICY=node-unlimited`,
  `S2_RDMA_CAPABILITIES=none`, `S2_RDMA_DEVICES_PER_NODE`; `drop: [ALL]` stays, no IPC_LOCK.

## api 0.11.95, platform chart 0.1.11

(0.11.95 = 0.11.94 (the operation listing) plus the two registration checks below; the chart carries the one value
behind them. jobs 0.1.26 unchanged; no migration.)

- Endpoint registration refuses, with an actionable 400, what would store but never reconcile: a `timeout_s` above the
  fleet's Knative max-revision-timeout-seconds (a medical pipeline at 7500 s against the hardcoded 3600: ReconcileFailed
  on every region), and `env` / `env_field_refs` / `weights.env` names Knative Serving reserves (`PORT`, `K_SERVICE`,
  `K_CONFIGURATION`, `K_REVISION`; Knative injects the serving port into `PORT`). Run classes are unchanged.
- Platform chart 0.1.11: `knative.maxRevisionTimeoutSeconds` (default 3600, unchanged behaviour) renders Knative's
  `max-revision-timeout-seconds` AND the API's `KNATIVE_MAX_REVISION_TIMEOUT_S` from the one value, so the two never
  disagree; `fleet.knative.max_revision_timeout_s` (stack tfvars) sets it (7500 for the retained medical endpoint).
  Other budgets reviewed and left as they are: the gateway route to a predictor has no request timeout (`0s`), the
  ext-auth call 10 s, Knative's revision-timeout-seconds default 600 (every endpoint sets its own `timeout`), the
  deployment progress deadline 3600 s (startup, not requests).

## api 0.11.94

(0.11.94 = 0.11.93 plus the operation listing below; jobs 0.1.26 and platform chart 0.1.10 unchanged.)

- `GET /v1/operations`: the tenant's live operations are no longer cut to 500 BEFORE the owner filter (a caller with
  many foreign operations in the tenant saw none of its own), and operations whose Job expired (TTL) are listed from
  their durable attempt records (as `GET /v1/operations/{id}` already reports them), narrowed by the owner records
  (`operation_owners`) and the records' provenance (`model`). Authorization (tenant, owner or admin, the key's model
  allow-list) applies to every candidate before the cut. Stable order `created_at` desc, id desc; bounded paging with
  `cursor=` (opaque, from the `X-Next-Cursor` response header; absent on the last page); `X-Regions-Skipped` names the
  regions that could not be read. The array response and `limit`/`model` are unchanged (client-compatible). No new
  ledger: the records and owner rows that exist.

## api 0.11.93

(0.11.93 = 0.11.92 without the last post-admission rollback; jobs 0.1.26 and platform chart 0.1.10 unchanged.
0.11.90–0.11.92 were never applied.)

- Continuations: a builder that fails after its admission never rolls the member back any more, not even on a
  definite 4xx: that 4xx proves only this caller's create was refused, while another replica that admitted the same
  intent (one shared member) may have created the operation. In every case the member stays a pending admission:
  the Job found is returned (whoever created it), otherwise the same request replays the intent. Regression: both
  replicas observe the prior head, A pauses after its admission, B replays and creates the Job, A's create is refused
  with a 4xx: nothing rolled back, one Job, one member.

## api 0.11.92

(0.11.92 = 0.11.91 with its automatic retirement WITHDRAWN after review; jobs 0.1.26 and platform chart 0.1.10
unchanged. Neither 0.11.90 nor 0.11.91 was applied.)

- Continuations: nothing is retired automatically any more (91's two-phase mark had a gap: the reaper's rollback was not
  compared against the mark it had observed, and a post-create cancel is no proof a Job never ran). A lineage head
  that was admitted but has no Job and no attempt record stays a PENDING admission; the lineage continues only by the
  same intent: the builder that renders that very operation name (the deterministic `-x<n>` / `-r<n>` of a replayed
  request) is admitted as a replay of the pending member and creates its Job (AlreadyExists is the same Job); any other
  continuation is refused (409 naming the pending admission). Two replicas racing on one continuation converge on one
  Job and one member whatever the interleaving (regression: the barrier with B's request inside A's paused create).
  An ambiguous failure after admission (5xx, transport) keeps the member; only a definite 4xx rolls back (unchanged
  from 91). `key_run_members.retire_marked_at` (migration 23) stays as a reserved, unused column.

## api 0.11.91 (unreleased candidate, superseded by 0.11.92)

(0.11.91 = 0.11.90 with its continuation reconciliation corrected after review; jobs 0.1.26 and platform chart 0.1.10
unchanged. 0.11.90 was never applied.)

- Continuations, the admission/create race (migration 23): immediate absence of a member's Job is never taken as
  "never created" (another replica may be paused between its admission and its create, or the API server may still be
  accepting a timed-out create). A member whose Job exists nowhere and that no attempt reported is MARKED first
  (`key_run_members.retire_marked_at`, durable, visible to every replica) and the continuation refused with the mark
  named; only an absence persisting `RETIRE_GRACE_S` (300 s) past the mark rolls it back. The admitting replica
  confirms its create against the mark (`keys.confirm_member_created`): a mark is cleared by the Job's existence; a
  member retired meanwhile makes the creator take its fresh Job back and answer 409 (replay). An ambiguous failure
  after admission (5xx, transport) keeps the member whatever an immediate read says (the Job found is returned as
  created); only a definite 4xx rolls back. Regressions: the barrier reproduction (replica B asking while A is paused
  between admission and create: B marks and refuses, A lands and confirms, one Job, one member), the opposite
  interleaving (A lands after B's rollback: A's Job taken back, 409), phase-one mark then phase-two retirement past
  the grace, an expired head on record kept, 5xx keeps the member.

## api 0.11.90

(0.11.90 = 0.11.89 plus the two items below; jobs 0.1.26 and platform chart 0.1.10 unchanged.)

- Endpoint children, the owner's readback: `GET /v1/operations/<run>/children/<unit_id>` (scope `read:operations`,
  the operation's owner; `<run>` the root or any member, the unit keyed by the durable root) returns the same
  authoritative readback the live attempt gets, without the attempt-scoped download URL; the child's own Operation
  carries `child: {root, unit, parent}` (docs/JOBS.md "Endpoint children").
- Continuations (resume, export retry, fallback, recovery, finalizer): a builder that fails after the admission is
  reconciled against the cluster before anything is rolled back: a create whose answer was lost keeps its member and
  the call returns the created Job (202); only a definite refusal (4xx) or a confirmed absence rolls the member back;
  an unreachable cluster keeps it. A lineage head whose Job exists nowhere, with no attempt record and an active member
  (the trace of a create that never happened) is retired, bounded, so the lineage continues from the real head instead
  of refusing every continuation with "continued by <phantom>"; a head that ran and expired (on record) is kept.
  Before, every exception after admission rolled the member back, so a continuation created with a lost answer ran
  unaccounted (and an export retry was rendered twice).

## api 0.11.89

(0.11.89 = 0.11.88 plus the endpoint resources addendum below; jobs 0.1.26 and platform chart 0.1.10 unchanged.)

- Endpoint `resources` keep optional `cpu_limit` / `memory_limit` above the `cpu` / `memory` requests and an
  optional `ephemeral_storage` request with `ephemeral_storage_limit` (= the request unless given), the names a run
  class uses, through the catalog projection and the native renderer (requests/limits on the endpoint container,
  single- and multi-node); defaults unchanged (4 / 16Gi, requests = limits, no ephemeral storage), nothing rendered
  without them (docs/API.md). The two-container shapes retained by the migration (NIM 20 CPU/160Gi + adapter 2/2Gi
  with 8704Mi/13Gi ephemeral; NV-Segment-CT 8/64Gi requests, 24/128Gi limits, 4Gi/16Gi ephemeral) project as one
  native container.

## api 0.11.88, jobs 0.1.26, platform chart 0.1.10

(0.11.88 supersedes the unreleased 0.11.87 candidate after review; everything of it is included.)

- Endpoint children, the capture: the child is rendered as a native application-I/O endpoint call (docs/JOBS.md
  "Endpoint children"): its request in a platform-written private binding (inline `input`, or `input_ref.sha256` +
  a renewable `transport.input_url` the caller verifies against the bound sha256 before posting), ONE inference POST
  per operation (the durable marker; a lost response is an unknown outcome, never a second POST), the response bytes
  and their manifest (sha256, media type, size, status, producing attempt) durable on the retained work volume before
  delivery to a platform-owned destination `s3://<bucket>/operations/<child>/{response.bin,call.json}` through
  presigned PUT URLs the API mints on demand as the child's refresh broker (`POST /internal/operations/<child>:refresh`,
  the child's own operation-scoped token, a live attempt of the child or of its transfer-only export retry required).
  No tenant or bucket credential reaches the parent or the caller. `:export` of a child is admitted as a `child` member
  after the child's own head (`keys.child_head`; no second request) and served by the same broker.
- Endpoint children, the request: the bound request is `{model, input | input_ref.sha256, timeout_s, region}`
  (not the input alone); renewable URLs are outside it; the allow-list and bound are the ones frozen on the parent's
  Job (no fallback to the current class); the child model's image is recorded at admission and an intent is never
  completed against another image.
- Endpoint children, the readback: authoritative past the Job's TTL: the durable row, the capture manifest from the
  destination (sha256, media type, size, HTTP status, attempt), a one-hour presigned download URL for the live
  attempt of the root (attempt-scoped, one object), the latest attempt record, and `outcome` (`succeeded` | `failed`
  | `undelivered` + `exportable` | `unknown` | `pending`): exit 4/3 are unknown and not exportable, never "undelivered".
- Endpoint children, the lifecycle (migration 22): the row is a durable intent written before the member and the
  Job (`pending` → `active` → `ended`, reason recorded); a crash in between or an ambiguous create (the Job may exist)
  leaves the intent pending and the next replay or the sweep finishes it deterministically (found or created; one
  Job, one member); rollback only on confirmed absence + definite refusal (`keys.rollback_run_member` now also for a
  child member: its request leaves the root's reservation); a cancel is issued and recorded and the row stays open
  until the terminal settlement (billing's release joined with the lease; a never-started child settles at its
  deletion), so no next child starts while the old caller or worker drains; the sweep's cascade covers cancelled,
  finished-without-successor and gone parents (a finished parent with a successor is judged by the successor).
- jobs 0.1.26: call.sh verifies a fetched request against the binding's `input_sha256` (an endpoint child's bound
  input reference) before its one POST; other bytes mean no POST (exit 2). Nothing else of the caller changed.

## api 0.11.87, platform chart 0.1.10 (unreleased candidate, superseded by 0.11.88)

- Endpoint children (docs/JOBS.md "Endpoint children"; migration 21): an operator-allowlisted, bounded primitive by
  which a LIVE attempt of a run asks the platform for an asynchronous endpoint call as a child of its root. A run
  class declares `child_endpoints` (endpoint models) and `child_max_units` (explicit bound, required); the render
  projects a pod-bound ServiceAccount token and the control API's URL into `main` (no customer bearer in the worker).
  `POST /internal/operations/<run>/children` / `GET …/children/<unit>`: TokenReview of the parent's pod (namespace,
  name, exact UID), live-attempt and open-root fences, allowlist, deterministic child id per (root, unit), immutable
  request hash (same request → readback; different → 409), one active child per root, the parent's remaining
  deadline inherited, the root's owner/namespace/key inherited by hash: the child's caller carries only an
  operation-scoped credential (`Bearer s2-admitted.<token>`, the admission HMAC), which the gateway hook accepts by
  rebuilding the principal from the key record by hash while the child's reservation lives (generic authorize
  support; no customer or admin key stored or forwarded). Ledger: the root's slot shared, one request per child
  (`keys.prepare_run_member` kind `child`), the key's endpoint-call policy unchanged (a finite GPU-second budget gets
  the same 501 as any endpoint call), settled as every endpoint call; the root closes only when nothing is active. Cancel cascade on the cancel route and in the reconciler's sweep; ended children recorded.
  Platform chart 0.1.10 routes `/internal/operations` on the control API (the invariant in tools/check.sh covers it).
  Nothing of the static stage delegation, the endpoint-stage refusal or the graph bound changed.

## api 0.11.86, jobs 0.1.25

(0.11.86 / 0.1.25 supersede the unreleased 0.11.85 / 0.1.24 candidate after review; everything of it is included.)

- Shared scratch placement is constrained in the rendered Pod, not only judged: `shared_scratch_available` returns
  the eligible GPU-free shared-fs pools that hold the caller, and the render conjoins `serverless2.nebius/pool In
  [those]` into the required node affinity next to `shared-fs In [true]`; under CPU-quota saturation Kueue therefore
  queues the caller for its CPU pool instead of taking a shared-fs GPU flavor. The transfer-only export retry is
  judged the same way at its own creation and refused (409, the reason) when the leaf cannot be placed then, never
  rendered on a GPU flavor or on a per-call claim. Tests model Kueue with a saturated CPU flavor: the constrained
  render queues (no flavor), the unconstrained one would have taken the GPU flavor.
- `call.sh` checks the two jq writes themselves (exit status and that the file parses: a short write leaves a
  non-empty fragment) before the rename and the sync: the capture manifest (failure → outcome unknown, marker kept,
  nothing delivered, exit 4) and RESULT.json (failure → removed, no export state claimed, exit 6). Regression: a jq
  stub that writes a partial document and exits 1 at each boundary.

## api 0.11.85, jobs 0.1.24 (unreleased candidate, superseded by 0.11.86 / 0.1.25)

(0.11.85 / 0.1.24 supersede the unreleased 0.11.84 / 0.1.23 candidate after review; everything of it is included.)

- Shared scratch placement proven, not assumed: `shared_scratch_available` judges the caller's OWN requests (main,
  uploader, the provisioning init; no GPU) against the per-node fit of the region's GPU-FREE pools that mount the
  shared filesystem (`fit.fitting(..., shared_fs=True, gpu_free=True)`): a region whose only shared-fs pools are GPU
  pools falls back to the per-call claim ("only GPU pools of <cluster> mount the shared filesystem": a Kueue-managed
  caller would otherwise be admitted on a GPU flavor and occupy a GPU node for 100m, or stay Pending where no flavor
  matches), as does a GPU-free shared-fs pool too small for the caller, and a claim that exists but is not Bound.
  The shared-fs expression is added to the pod's required node affinity by conjunction with whatever the execution
  profile already requires, never by overwriting it.
- `call.sh` fails explicitly at every durability boundary (the script runs without errexit, so the 0.1.23 `durable`
  calls were advisory): the one-POST marker not durable → no POST, marker removed, exit 6 (retryable; a marker that
  could not even be removed makes the next attempt refuse to post); the captured response not durable → outcome
  unknown for good, nothing delivered, exit 4 (never re-posted); the manifest not durable → nothing delivered,
  exit 6 (a fresh attempt delivers the durable capture without posting); RESULT.json not durable → removed, no
  export success claimed, exit 6. Tests drive the caller with a failing `sync` at each boundary and through the
  restart that follows.

## api 0.11.84, jobs 0.1.23 (unreleased candidate, superseded by 0.11.85 / 0.1.24)

- Durable endpoint calls on the tenant's shared scratch (docs/JOBS.md "Scratch storage"), opt-in per endpoint model
  (`call_scratch: shared`; default `network` unchanged): the caller's capture lives on the tenant's existing
  `scratch-shared` claim at the API-derived leaf `calls/<op>` (a subPath; main and the uploader never see the tenant
  directory) instead of a per-call block claim whose attach cost ~50 s per call. A trusted init container provisions
  exactly that leaf (root with CHOWN/FOWNER/DAC_OVERRIDE only). `call.sh` fsyncs the one-POST marker, the capture,
  the manifest and RESULT.json (jobs 0.1.23). The uploader empties the leaf only after the acknowledged delivery and
  a proof of the capture (manifest, sha256, RESULT.json; `scratch.sh`), removes symlinks as links and never follows
  them, refuses foreign-owned entries, never deletes the shared claim; the empty leaf is the recorded GC gap (no
  age-based reaping, no scanning of other work). `:export` and a CPU delivery retry mount the same leaf. Fallback to
  the per-call claim, recorded on the Job, where the region/cluster/namespace cannot carry it. `tools/scratch-test.sh`
  in `make check`. Includes 0.11.83 (sessions cold attachment) and 0.11.82.

## api 0.11.83

(0.11.83 supersedes the unreleased 0.11.82 candidate: tags are immutable; everything of 0.11.82 is included.)

- Cold attachment of a live session (docs/API.md "Sessions", migration 20): the attach wait no longer runs from the
  hook's bind (before the endpoint has a pod) but from the first Ready replica the reconciler observes for the
  session's endpoint in its region, recorded ONCE (`ready_seen_at`, a compare-and-swap; a later readiness flap never
  moves it) together with `attach_deadline_at` (`attach_wait_s` after it, capped by the admitted `deadline_at`).
  The hard bound stays the admitted deadline: a session that never sees a Ready replica ends `unattached` there,
  no extra reservation, no extension. Endpoint status is read once per (model, region) per sweep; a status that
  cannot be read is unknown (neither Ready nor idle: nothing changes). The public session carries `phase`
  (`claimed`, `starting`, `waiting_for_worker`, `attached`, `ended`), `ready_seen_at` and `attach_deadline_at`, no
  token, no pod identity; no GPU time is advertised for a session that never attached. PostgreSQL-backed tests:
  cold start longer than the wait then ready and attach; ready but no worker expires at the attach deadline;
  never-ready ends at the admitted deadline; a flap never extends; two regions judged separately; concurrent
  sweeps record readiness once and end a session once.

## api 0.11.82 (unreleased candidate, superseded by 0.11.83)

- Sessions (docs/API.md "Sessions"), before any enablement: `POST /v1/models/<id>:session` refuses a `timeout_s`
  above the endpoint's transport deadline (a single-node endpoint's revision timeout, the model's `timeout_s`, 600 s
  by default; a multi-node endpoint has none) with a 400 naming the bound, instead of admitting a stream Knative
  would cut mid-way with no record; nothing is shortened silently; `timeout_s` left out now defaults to that
  deadline (3600 without one) instead of a fixed 3600 that the transport could not carry. The idle shutdown of multi-node endpoints counts
  bound and attached sessions as activity. The no-reconnect rule is stated as kept. PostgreSQL-backed tests of the
  session transitions (compare-and-swap bind across replicas, attach fencing, release idempotency, cancel) and of
  the sweep (expired claim, unattached binding, deadline cancel request, lost worker, deferred settlement) run in
  `make check` like the lease tests. Sessions stay off (`sessions.enabled` false); no interface changed.

## api 0.11.81, fleet chart 0.1.12

(0.11.81 supersedes the unreleased 0.11.80 candidate after review: tags are immutable, so the review fixes below are a
new tag; everything of 0.11.80 is included.)

- Review fixes to the per-node fit and the unschedulable switch: (1) the assigned pool of an admitted pod is judged on
  the cluster that admitted it only (`fit.assigned_pool_holds(pod, cluster)`): the same pool label can be a bigger
  preset in another cluster and must not mask an impossible actual worker. (2) The shared-filesystem and local-NVMe
  requirements are intersected per (cluster, pool) BEFORE a pool counts as a candidate (`fit.candidates`,
  `fit.fitting(..., shared_fs, local_nvme)`): a node big enough but without the mount (or the NVMe) the run needs is
  no candidate, the affinity never names a set of pools no single node realizes, the refusal names the capability
  ("on one node with the shared filesystem") and the largest candidate is the largest capable one. (3) The fit is
  computed on the FINAL rendered pod, after the execution profile reshaped it (an application io mode drops the
  fetch init container), never on the shell before it. (4) The snapshot-to-fence window of
  `jobs.switch_unschedulable` is bounded from both sides: the worker's pods are re-read immediately before the fence
  (an attempt that bound meanwhile stops the switch: 409, nothing fenced, nothing created) and once more after it by
  the snapshot's pod UIDs (a binding in the last instants is recorded on the original as
  `serverless2.nebius/started-before-fence` {pod, uid, node, phase, started_at} and in both `recovery` reasons,
  "its records are retained": the successor mark comes last, so the attempt's uploader records stay accepted);
  nothing is pretended never-started.

## api 0.11.80, fleet chart 0.1.12 (unreleased candidate, superseded by 0.11.81)

- Per-node fit (docs/SCHEDULING.md "Per-node fit"). Kueue admits against a flavor's aggregate quota and never checks
  that one node holds the pod: on 2026-10-14 an AlphaFold 3 MCP pipeline (16.1 CPU, 64.25 Gi, 32 Gi ephemeral,
  GPU-free) was refused by the CPU pool's ephemeral quota for the moment, fell through the flavor order to
  `h100-reserved-1x`, whose aggregate CPU covered it while its nodes admit 14, and stayed Pending for ever holding
  that flavor's quota. Fleet chart 0.1.12 publishes `fit.yaml` in the fleet document (what one node of every pool
  admits, from the quota helpers with one node); the render constrains every run to the pools whose node holds the
  pod's effective requests (cpu, memory, ephemeral-storage, GPUs; main + uploader; conjunctive with the explicit
  `pool:`, the class pools, `shared-fs` and `local-nvme`; placement regions narrowed to clusters with a fitting pool)
  and refuses at submission a shape no candidate pool holds (400 `impossible shape`, naming the largest candidate
  node); an explicit `pool:` that cannot hold the pod is refused, never widened. A fitting pool whose quota is busy
  stays QUEUED (temporarily unavailable). Nothing is reduced or enlarged on the caller's behalf. `tools/check.sh`
  asserts `fit.yaml` against the ClusterQueues on every cluster. Without `fit.yaml` nothing changes.
- Same-root recovery of an admitted-but-unschedulable run (`reconciliation.unschedulable_switch`,
  `jobs.switch_unschedulable`): every pod Pending and unbound, the newest `Unschedulable` for `UNSCHEDULABLE_WAIT_S`
  (600 s), the assigned pool unable to hold it → a successor `<root>-r<n>` with the fit affinity from the same input,
  placement, class and key, the original fenced exactly as a fallback (`SUPERSEDED`, Workload deactivated, Job
  suspended), lineage and the owner's handle preserved, a failed create restores the original; a pool that can hold
  the pod is left waiting, a started attempt is never switched, an impossible shape is annotated once
  (`serverless2.nebius/unschedulable`), at most two switches per root. Topology-Aware Scheduling is left for its
  own qualification.
- The gateway hook's direct-POST log line names the model only (no key alias, no tenant): the shared telemetry
  carries no customer identities. The admitted-POST line (operation, attempt) is unchanged.

## api 0.11.79, platform chart 0.1.9

- Operation status/result stay readable when usage is unknown: `billing.py` settles a run or a native endpoint call
  that ended without a trustworthy measurement with the literal `unknown` in the `<LABEL>/gpu-seconds` and
  `<LABEL>/billed` annotations (the key's reservation is kept as unknown GPU time, never zero), but `status.normalise`
  and the chain view parsed both with `float()` and raised `ValueError` → `GET /v1/operations/<id>` and `:result`
  answered 500 (seen on a Cosmos native call on 0.11.78: readiness 200, one inference POST 403, the response uploaded,
  the owner's status unreadable). One parser (`status.measured`) now reads the accounting annotations: a finite
  non-negative number is the measurement; absent, `unknown`, NaN/inf, negative or any malformed value is `null`,
  never zero and never an error. The operation carries `usage`: `measured`, `unknown` (settled without a
  measurement), `pending` (not settled yet), so a `null` is distinguishable from "not yet". No interface removed;
  the annotations and the billing writer are unchanged.
- Platform chart 0.1.9, the control API's public HTTPRoute (`charts/platform/templates/api/api.yaml`): the worker
  routes `/internal/executions` (native execution leases: attach, poll, release) and `/internal/sessions` (live
  sessions) are routed to the API. 0.1.8 routed only `/internal/attempts` and `/internal/authorize` and answered
  every other `/internal` path with the 404 direct response, so a worker's `POST /internal/executions/<op>:attach`
  through `S2_API_URL` got 404 from Envoy (no upstream), the shared client surfaced it as a refusal and the worker
  failed closed: every native-lease endpoint call was refused (Cosmos first case on 0.11.78, proven from the control
  gateway's access log). Authentication of those routes is unchanged (the pod's projected token, TokenReview,
  nothing from headers); the `/internal` catch-all stays a 404 for everything else; the per-key rate limit of the
  route applies per worker token as it does to `/internal/attempts`. `tools/check.sh` asserts the four prefixes and
  the catch-all on every cluster's render.
- Marker diagnosability at the gateway hook (docs/API.md "Native execution leases", markers row): one log line per
  admitted inference POST naming the operation and attempt whose markers were emitted (never per readiness probe),
  and one per direct POST to a `native_lease: required` model (no markers: the worker refuses it, as before). The
  direct-call policy is unchanged.

## platform stage: the image cache's registry static key is an environment input, never issued by a plan

- `stack/platform/secrets.tf` no longer runs `stack/scripts/registry-static-key.sh` as an `external` data source.
  That data source issued a real static Container Registry key (`nebius iam static-key issue`) during `terraform plan`
  whenever its gitignored cache (`stack/.secrets/<name>-registry-key.json`) was absent: a fresh checkout or a second
  worktree minted a credential on a read-only plan, two clusters planned in the same minute collided on the key's
  name (`<name>-image-cache-YYYYmmddHHMM`), and each cluster's plan then proposed its own rotation of
  `registry/zot-sync-credentials` (seen on the api 0.11.78 freeze: three plans, none applied). The key is now the
  fourth secret input, `secrets.registry_key_env` (default `REGISTRY_STATIC_KEY`), read from the environment by
  `stack.sh` like the NGC key: required on every cluster while `images.cache.enabled`, refused before any plan when
  unset, one value per fleet, and a `precondition` on the Secret stops a direct `terraform` call without it. The
  `hashicorp/external` provider requirement is dropped. `stack.sh` asks for the secret inputs only where the platform
  stage is planned, applied or destroyed (`plan|apply|destroy all|platform`, `tf platform <id> -- plan|apply|destroy`);
  `plan cloud`, `output`, `registry`, `clusters` and read-only `tf` calls run without them, so the key's own bootstrap
  (the script discovering the hub project and ops SA through `output cloud`) does not require the key it is about
  to issue. `tools/registry-key-test.sh` (part of `check tests`) runs the launcher and the script against a fake
  Nebius CLI and a fake Terraform: discovery and the cloud plan without the key, the platform plan refused without it
  and run with it (`TF_VAR_registry_key`), the no-key bootstrap issue, refuse/rotate/id-fallback/import, and an
  import blocked by the lock.
- `stack/scripts/registry-static-key.sh` is an explicit operator command now (`<fleet name> [--rotate]`): it issues the
  key once (the name carries a random suffix, never the minute), under a lock, and prints the `export` line and the
  key's id; it refuses to issue a second key for a fleet whose key file it already holds unless `--rotate` is given;
  issue and import share one lock per fleet.
  The id is read from the response's `operation.resource_id` (`IssueStaticKeyResponse` is `{token, operation}`; the
  former parser read `metadata.id` and recorded `""` for every key it issued) and, failing that, resolved from the
  token with `nebius iam static-key find --token` (the exact ownership proof of any key: its own token names it;
  `FindStaticKeyResponse` wraps the key under `static_key`, so the id is `static_key.metadata.id`: a first version of
  this follow-up read a top-level `metadata` and recorded `""` on import).
  Its file lives under `~/.config/serverless2/<name>/registry-key.json` (0600), never in a checkout.
- Migration for a fleet with the old cache: `export REGISTRY_STATIC_KEY="$(python3 -c 'import json; print(json.load(open("stack/.secrets/<name>-registry-key.json"))["key"])')"`
  (or run the script's `--import stack/.secrets/<name>-registry-key.json`), then `./stack.sh plan` shows no change to
  `registry/zot-sync-credentials`. Keys the old data source issued and never applied: list them with
  `nebius iam static-key list-by-account --account-service-account-id <ops SA>` and delete by id.

## api 0.11.78, platform chart 0.1.8

- Lease clients (docs/API.md "Lease clients"): on a fleet with a CA bundle, the bundle is projected as a FILE
  (`/var/run/secrets/serverless2/ca-bundle.pem`, a second source of the `s2-lease` projected volume, from the
  endpoint's own ConfigMap `<name>-trust` in `models`, applied before and removed with the endpoint's other objects)
  and named by the standard variables `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `AWS_CA_BUNDLE` (the
  async caller's set) plus `S2_CA_FILE`. `S2_CA_PEM` is no longer rendered: the full bundle (467 KB on the live
  fleet) as an environment value exceeded the kernel's single-argument bound and the container never execed
  (`exec /app/.venv/bin/python: argument list too long`). The bundle is projected whole (a render refuses above the
  ConfigMap bound instead of cutting it). The shared client is unchanged (`services/lease-client/s2lease.py`,
  sha256 `f96b274b…53885`): `ssl.create_default_context()` reads `SSL_CERT_FILE`, so an image built with it trusts
  the fleet without a rebuild; verified against a live TLS listener in the tests. Platform chart 0.1.8: the two
  endpoint Roles in `models` (`serverless2-api-endpoints`, `serverless2-api-agent-models`) gain `configmaps`
  get/list/create/patch/delete (never secrets; `tools/check.sh` asserts the bound).

## api 0.11.77

- Endpoint scratch (`empty_dirs`): a mount path may contain dots in its components (`/app/.cache`, an engine's hidden
  cache directory, is ordinary bounded scratch); the path must still be canonical and absolute: no `.` or `..`
  component, no empty component (`//`), no trailing slash, not `/`; the reserved roots and the weights/shm overlap are
  refused as before. Interfaces unchanged.

## api 0.11.76

- Graph "action required" (docs/API.md "Operations"): the root handle's `graph` carries `action_required`, `blocked_on`
  (each FAILED lineage head with its attempt, the two actions `:resume <operation>` / `:cancel <root>`, and its parked
  dependents) and `waiting` (dependency-gated stages); a QUEUED member reports `gated` when the gate parks it. The root
  reads `phase: action_required` with the remedy in `reason` when the graph is open, nothing is active (admitted QUEUED,
  RUNNING, FINALIZING or a delivery retry) and a stage FAILED; `status` stays RUNNING (resumable), nothing is failed or
  retried, the gate and the ledger are untouched. An active sibling suppresses the signal; a resume clears it.

## api 0.11.75 (supersedes the unapplied 0.11.74)

- Restart from inputs, placement-correct: the admission record keeps the run's MultiKueue `managedBy`, so the restart of
  an expired fleet-placed root is a manager Job placed by the fleet again (never a plain Job on the manager's cluster,
  never a worker pin from before); a single-cluster root restarts on its own cluster. The terminal state comes from the
  durable attempt rows: a SUCCEEDED root is never restarted (409, its result stands); CANCELLED and FAILED may be.
  Regressions: fleet-manager admission → worker → TTL → restart; SUCCEEDED refused; stale handle after the restart names
  the head; another principal 404; the same idempotency key replays the same continuation.

## api 0.11.74

- Restart from inputs (docs/JOBS.md "Resume"): every run admitted from this version on leaves an admission record in the
  ledger (migration 19: the Job's labels and annotations, region, timeout, output prefix), kept past the Job's TTL.
  `:resume` of a FAILED root whose Job and work volume expired renders it again from that record on a fresh volume on
  the same cluster when the caller passes `restart_from_inputs: true` (or a `checkpoint_ref`), as a continuation of the
  same root (lineage, handle and graph follow; owner, key and class binding inherited; the new render gets the current
  placement rules). Without the flag: 409 saying what to pass; a root admitted before this version, with no record:
  404 "submit a new run", never a reconstruction. Nothing else changes against 0.11.73.

## api 0.11.73

- Export retries and finalizers keep the shared-filesystem placement: the transfer render drops the compute attempt's
  class, pool and NVMe constraints and re-adds exactly the `serverless2.nebius/shared-fs` requirement when the volumes
  it retains (weights, data, shared checkpoints) live on the shared filesystem, recomputed from those volumes, never
  from the stripped restricted assets. Without it a delivery of a class that mounts weights/data landed on an unmounted
  system node again (root review of 0.11.72). Nothing else changes.

## api 0.11.72, fleet chart 0.1.11, cluster module: filesystem-aware placement

- A run that mounts the region's shared filesystem (`weights`, `data`, `checkpoints: shared`, `assets`) now requires a
  node that mounts it: the cluster module labels every node group `serverless2.nebius/shared-fs=true|false` by its
  attachment (GPU pools true when the region has the filesystem, CPU pools by `shared_filesystem`, the system pool only
  with `mount_on_system`), every worker flavor carries the same label in `nodeLabels`, and the API renders a required
  node affinity on such runs, so Kueue never assigns them the system flavor of an unmounted system pool (the FailedMount
  of 14 Oct). Runs that mount none of these are unchanged. docs/FLEET.md "Filesystem-aware placement" and
  docs/OPERATIONS.md "Repair a run admitted on an unmounted flavor" (the scoped repair of an existing Job, no new
  controller). check.sh asserts the flavor label; module tests cover the label.

## api 0.11.71

- Export of a run class with work mount aliases: the transfer-only export render read the finalizer's attempt record
  (UnboundLocalError on 0.11.63–0.11.70 for a class with aliases and no finalizer); the export now keeps the rendered
  alias expression (its main only exits 0), and the literal finalized pod name is set on the finalizer's main, where it
  belongs. Nothing else changes against 0.11.70.

## api 0.11.70

- Restricted assets: a class may name up to eight assets (was four; work mount aliases keep four), one bound for the
  registration and the render (`jobs.MAX_ASSETS`). BindCraft declares five. The registry, grant, reserved-path,
  overlap and single-node guards are unchanged. Nothing else changes against 0.11.69.

## api 0.11.69

- Restricted assets are main-compute-only (root review of 0.11.68): a transfer-only export retry and the application
  finalizer, which render the class again on the kept volume with a CPU container in main's place, drop the asset
  volumes, main's asset mounts, the supplemental groups they brought and the identity annotation; the compute Job is
  unchanged. Nothing else changes against 0.11.68.

## fleet chart 0.1.10, cluster module: CPU pools scheduled and mounted

- CPU pools (docs/FLEET.md "CPU pools"): `regions.<r>.cpu_pools` now reach the scheduler: one Kueue ResourceFlavor per
  pool (node label, the pool's taint tolerated, no GPU toleration) listed in every ClusterQueue after `system` with
  `nvidia.com/gpu: 0` and CPU/memory = max_nodes x preset minus the per-node reserve, on the workers and mirrored on
  the manager; a GPU-free run that does not fit the system nodes is admitted there and the pool scales from zero. The
  cluster module attaches and mounts the region's weights filesystem on the pool's nodes like a GPU pool
  (`shared_filesystem`, default true), so `weights`/`data`, shared checkpoints and restricted assets work there.
  tfvars validation of the preset shape; check.sh asserts the flavor order and tolerations. No API change.

## api 0.11.66 (= 0.11.64 + the 0.11.67 lineage hotfix), platform chart 0.1.7, jobs 0.1.22
## api 0.11.68, fleet chart 0.1.9, tenant chart 0.3.4 (= 0.11.64 + the 0.11.67 lineage hotfix + restricted assets)

- Restricted assets (docs/JOBS.md "Restricted assets"): an operator registry in the fleet document (`assets`: file or
  pinned tree on the weights filesystem, gid, sha256, size), a per-tenant grant (`tenants.<name>.assets`) that the
  tenant chart renders as a static read-only volume rooted at the asset itself (`hostPath` File/Directory, claim
  `asset-<alias>`), and a run-class binding (`job.assets` / spec `assets: [{alias, mount_path}]`) that mounts the claim
  on `main` only, read-only twice, at a bounded path overlapping no other mount, with the asset's group as a supplemental
  group; no fsGroup rewrite (hostPath volumes are never chowned); identity recorded on the Job. `weights`/`data` paths that
  reach a registered asset (itself, below it, or an ancestor) are refused. Single-node runs only.

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
