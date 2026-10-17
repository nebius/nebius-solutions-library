"""Stage composition (docs/JOBS.md "Stage composition"): a root run declares, at its submission, the stages that will
follow it (`execution.graph`: stage id -> dependencies); each stage is submitted later as its own run
(`execution.stage = {root, id}`), on the root's key and in the root's ledger, and waits (a suspended Job without a
Kueue queue) until every dependency delivered. The gate (`gate`, the control API's reconcile loop) then binds the
dependencies' results into the stage's pod (`DEPENDENCY_RESULTS`), delivers the stage's own private binding (never
before: request-scoped credentials are not consumed while it waits), and hands the Job to Kueue by adding the queue
label. A dependency that failed or was cancelled cancels the stage; the root's cancel cancels every stage; the graph's
deadline cancels what still waits. The root's run (its key slot and request) closes only when every declared stage
settled (keys._close_run_if_idle) or the graph was cancelled; the root's handle reports the graph.

Only what the gate writes changes a waiting Job: the queue label, the waiting label, and the pod template's
annotation that carries the results (mutable on a never-started suspended Job: KEP-2926). A completed stage is never
run again: a replayed submission of a stage that settled returns its durable state."""

import json
import logging
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

import attempts
import catalog
import config as cfg
import db
import execution
import jobs
import keys
import kube
import status
from auth import Principal, check_operation_owner
from config import REGION
from resilience import iso, parse_ts, retry

log = logging.getLogger("api")
LABEL = cfg.LABEL
QUEUE_LABEL = "kueue.x-k8s.io/queue-name"
# The queue a waiting stage names: a LocalQueue that never exists in any tenant namespace (charts/tenant refuses a
# profile of this name). Kueue creates a Workload for it that is never admitted and holds no quota, and keeps the Job
# suspended; the label must be present, because Kueue's LocalQueueDefaulting injects `default` into a label-less Job
# when the namespace has a LocalQueue `default` (the tenants' default profile), which admitted a waiting stage on API51.
WAITING_QUEUE = "serverless2-stage-waiting"
WAITING = f"{LABEL}/stage-waiting"  # label "true" while the stage waits for its dependencies
STAGE_ROOT = f"{LABEL}/stage-root"  # label: the root the stage belongs to
STAGE_ID = f"{LABEL}/stage"  # annotation: the stage's declared id
DEPENDS_ON = f"{LABEL}/depends-on"  # annotation: the declared dependencies, comma-separated
WAIT_REASON = f"{LABEL}/stage-waiting-reason"  # annotation: why it still waits (the gate's last finding)
ADMITTED_AT = f"{LABEL}/stage-admitted-at"  # annotation: when the gate handed it to Kueue
RESULTS = f"{LABEL}/dependency-results"  # pod template annotation: the dependencies' results (DEPENDENCY_RESULTS)
RESULTS_ENV = "DEPENDENCY_RESULTS"
ROOT = execution.ROOT_STAGE
TERMINAL_FAILED = ("FAILED", "CANCELLED")
DEFAULT_DEADLINE_S = 14 * 86400  # the long-run contract; `deadline_s` up to 30 days


def stage_name(root: str, stage: str) -> str:
    """A stage's operation id: deterministic (one operation per declared stage; a resubmission is a replay)."""
    return f"{root}-s-{stage}"


def dependencies(graph: dict, stage: str) -> list[str]:
    """The declared dependencies of a stage; a stage with none depends on the root."""
    deps = list((graph.get("stages") or {}).get(stage) or [])
    return deps or [ROOT]


def mark_waiting(job: dict, root: str, stage: str, deps: list[str]) -> None:
    """Turn a rendered run into a waiting stage: suspended, its Kueue queue label naming a queue that does not exist
    (WAITING_QUEUE: no admission, no quota; the gate later sets the real profile, allowed while the Job is suspended),
    stage labels and annotations, and the main container's DEPENDENCY_RESULTS from the pod template's annotation
    (filled by the gate). A JobSet stage is refused (its template is not patchable the same way)."""
    if job.get("kind") != "Job":
        raise HTTPException(400, "a stage must be a single-node run (multi-node stages are not supported)")
    md = job["metadata"]
    md.setdefault("labels", {})[QUEUE_LABEL] = WAITING_QUEUE  # never a real queue: Kueue admits nothing for it
    md["labels"][WAITING] = "true"
    md["labels"][STAGE_ROOT] = root
    md.setdefault("annotations", {})[STAGE_ID] = stage
    md["annotations"][STAGE_ROOT] = root
    md["annotations"][DEPENDS_ON] = ",".join(deps)
    job["spec"]["suspend"] = True
    bind_results(job, "{}")


def inherit(job: dict, ann: dict) -> None:
    """A continuation (resume, export retry, finalizer, fallback, recovery) of a stage stays that stage: the stage
    label and annotations follow it (never the waiting label: a continuation exists only after the stage was
    admitted), and the dependency results bound into the original are bound into the successor's pod again."""
    if not ann.get(STAGE_ID) or not ann.get(STAGE_ROOT):
        return
    md = job["metadata"]
    md.setdefault("labels", {})[STAGE_ROOT] = ann[STAGE_ROOT]
    md.setdefault("annotations", {})[STAGE_ID] = ann[STAGE_ID]
    md["annotations"][STAGE_ROOT] = ann[STAGE_ROOT]  # the next continuation inherits from this one's annotations
    if ann.get(DEPENDS_ON):
        md["annotations"][DEPENDS_ON] = ann[DEPENDS_ON]
    if ann.get(RESULTS) and job.get("kind") == "Job":
        md["annotations"][RESULTS] = ann[RESULTS]
        bind_results(job, ann[RESULTS])


def bind_results(job: dict, results_json: str) -> None:
    """The dependencies' results into the pod: the template annotation the main container reads (downward API)."""
    tmpl = job["spec"]["template"]
    tmpl.setdefault("metadata", {}).setdefault("annotations", {})[RESULTS] = results_json
    for c in tmpl["spec"].get("containers") or []:
        if c.get("name") == "main":
            env = [e for e in (c.get("env") or []) if e.get("name") != RESULTS_ENV]
            env.append(
                {"name": RESULTS_ENV, "valueFrom": {"fieldRef": {"fieldPath": f"metadata.annotations['{RESULTS}']"}}}
            )
            c["env"] = env


def is_waiting(job: dict) -> bool:
    return (job.get("metadata", {}).get("labels") or {}).get(WAITING) == "true"


def stage_of(job: dict) -> dict | None:
    """The stage identity of a Job that is a stage (None otherwise)."""
    md = job.get("metadata", {})
    root = (md.get("labels") or {}).get(STAGE_ROOT)
    if not root:
        return None
    ann = md.get("annotations") or {}
    return {
        "root": root,
        "id": ann.get(STAGE_ID),
        "depends_on": [d for d in (ann.get(DEPENDS_ON) or "").split(",") if d],
        "waiting": is_waiting(job),
        "reason": ann.get(WAIT_REASON),
    }


# ---- the state of an operation, from what is durable, without a principal ---------------------------------------
def _find(ns: str, name: str, memo: dict | None) -> tuple[dict, str]:
    """`jobs.find`, remembered within one read or gate pass (`memo`): a graph's view and the gate ask for the same
    lineage heads several times (a dependency shared by every stage, the gated check after the state), each a Job
    read plus the pods of its worker (14 reads, 6.8 s on a 7-stage graph, api 0.11.110). A 404 is remembered too."""
    if memo is None:
        return jobs.find(ns, name)
    hit = memo.get(name)
    if hit is None:
        try:
            hit = memo[name] = ("ok", jobs.find(ns, name))
        except HTTPException as e:
            if e.status_code != 404:
                raise
            hit = memo[name] = ("404", e)
    if hit[0] == "404":
        raise hit[1]
    return hit[1]


def ledger_head(ns: str, operation: str) -> str | None:
    """The newest member of the operation's own lineage in the run ledger (keys.run_head / stage_head) when it is a
    compute continuation (a resume, fallback or recovery): the authoritative link, also for one admitted before api
    0.11.65 recorded successor marks on a manual resume. None when the ledger does not know the operation, when it is
    the head itself, when the head is an export retry or finalizer of it (`export-of` / `finalize-of`: the compute
    attempt stays the handle and reports the delivery, docs/JOBS.md "Attempts and finalization"), or when the head's
    Job cannot be read (never a blind jump)."""
    if not db.enabled():
        return None
    try:
        m = keys.member_of(operation)
        if not m:
            return None
        if m["kind"] == "stage":
            head = keys.stage_head(m["root"], m["stage"])
        elif m["kind"] == "child":  # an endpoint child: its own lineage (its export retries), never the run's head
            head = keys.child_head(m["root"], m["stage"])
        else:
            head = keys.run_head(m["root"])
    except Exception as e:  # noqa: BLE001 - an unreadable ledger leaves the handle on the named operation
        log.warning("ledger head of %s: %s", operation, e)
        return None
    if not head or head == operation:
        return None
    try:
        hj, _r = jobs.find(ns, head)
    except HTTPException as e:
        if e.status_code != 404:
            return None
        # the head's Job expired too (TTL): the durable attempt rows say what it was. An export retry or finalizer
        # records its role; a compute continuation records `run` (or nothing yet, when it never started): followed
        hj = None
    if hj is not None:
        ann = hj["metadata"].get("annotations") or {}
        return None if ann.get(f"{LABEL}/export-of") or ann.get(f"{LABEL}/finalize-of") else head
    if attempts.enabled():
        rows = [r for r in attempts.for_operation(m["root"]) if r.get("operation") == head]
        if any((r.get("role") or "run") in ("export", "finalize") for r in rows):
            return None
    return head


def head_of(
    ns: str, operation: str, memo: dict | None = None, follow: bool = True, visit=None
) -> tuple[str, dict | None | bool]:
    """The head of an operation's successor chain (a capacity fallback, a recovery, a manual resume), as the gate and
    the public handle follow it: the successor annotation of a live Job, else (the Job gone, TTL) the durable successor
    mark, with the run ledger's lineage head as the authority behind both (`ledger_head`). (head, its Job): the Job is
    None when it is gone (the durable rows are what remains, or nothing: not submitted yet; without the database a gone
    Job is followed nowhere), False when the chain is longer than 16 (never followed). `follow=False` reads the named
    operation only. `visit(name, job, region)` is called for every operation on the way, the named one and the head
    (job None when it is gone): the public handle's owner check."""
    current = operation
    for _ in range(16):
        try:
            job, region = _find(ns, current, memo)
        except HTTPException as e:
            if e.status_code != 404:
                raise
            if visit is not None:
                visit(current, None, None)
            if not follow or not attempts.enabled():
                return current, None
            nxt = attempts.successor_of(current) or ledger_head(ns, current)
            if nxt and nxt != current:
                current = nxt
                continue
            return current, None
        if visit is not None:
            visit(current, job, region)
        nxt = (
            ((job["metadata"].get("annotations") or {}).get(f"{LABEL}/successor") or ledger_head(ns, current))
            if follow
            else None
        )
        if nxt and nxt != current:
            current = nxt
            continue
        return current, job
    return current, False


def state_of(ns: str, operation: str, memo: dict | None = None) -> dict:
    """{"status", "result_ref", "attempt_id", "operation"} of an operation, as the gate sees it: the live Job
    (following fallback/recovery successors) with its durable attempt records, else the durable rows of an expired
    Job, else status None (nothing exists: not submitted yet). An export retry or finalizer that delivered after the
    Job failed counts (attempts.delivered)."""
    current, job = head_of(ns, operation, memo)
    if job is False or (job is None and not attempts.enabled()):
        return {"operation": current, "status": None}
    if job is None:
        tenant = ns[len(cfg.TENANT_NS_PREFIX) :] if ns.startswith(cfg.TENANT_NS_PREFIX) else ns
        op = attempts.synthesize(current, tenant)
        if op is None:
            # no Job, no attempt row: either never submitted, or cancelled before it started (its Job deleted; the
            # cancel mark and the settled ledger member are what remains)
            marks = attempts._marks(current)
            member = keys.member_of(current)
            if (marks and marks[1]) or (member and member["state"] != "active"):
                return {"operation": current, "status": "CANCELLED"}
            return {"operation": current, "status": None}
    else:
        root = (job["metadata"].get("annotations") or {}).get(f"{LABEL}/resumed-from") or current
        rows = [r for r in attempts.for_operation(root) if r["operation"] == current] if attempts.enabled() else []
        op = jobs.normalise(job, None, rows)
        if attempts.enabled():
            op.update(attempts.lifecycle(current, root, rows))
    if attempts.enabled() and op.get("status") in ("EXPORT_FAILED", "FINALIZING", "FAILED"):
        op.update(attempts.delivered(op, current, op.get("resumed_from") or current))
    return _state(current, op)


def _state(current: str, op: dict) -> dict:
    """The authoritative delivery identity: when an export retry or finalizer delivered the result, the delivering
    operation and its attempt and epoch (never the producer's compute attempt next to the finalizer's receipt); else
    the operation's own newest attempt. `producer` is the lineage member the result belongs to."""
    delivered_by = op.get("exported_by")
    return {
        "operation": delivered_by or current,
        "producer": current,
        "status": op.get("status"),
        "result_ref": op.get("result_ref"),
        "attempt_id": (op.get("exported_by_attempt_id") if delivered_by else None) or op.get("attempt_id"),
        "epoch": (op.get("exported_by_epoch") if delivered_by else None) or op.get("epoch"),
        "ended_at": op.get("ended_at"),  # when a FAILED head failed: the failure settlement's clock (gate)
    }


def dependency_head(root: str, dep: str) -> str | None:
    """The operation a dependency currently is: the newest member of its lineage (the root and its continuations for
    `root`; the stage and its continuations otherwise), so a resumed or recovered dependency is judged by what it
    became. None before the stage was submitted."""
    return keys.run_head(root) if dep == ROOT else keys.stage_head(root, dep)


def evaluate(ns: str, graph: dict, deps: list[str], memo: dict | None = None) -> tuple[str, dict | str]:
    """What a stage's dependencies allow: ("ready", {dep: result}) when every dependency SUCCEEDED with a result_ref;
    ("failed", why) when one was CANCELLED or succeeded without a result (not runnable); ("waiting", why) otherwise
    (not submitted, queued, running, an export still retried, or FAILED: a failed dependency may be resumed, so its
    dependents wait for its lineage until the root is cancelled or the graph's deadline passes)."""
    results, waiting = {}, []
    for dep in deps:
        head = dependency_head(graph["root"], dep)
        st = state_of(ns, head, memo) if head else {"operation": None, "status": None}
        if st["status"] == "CANCELLED":
            return "failed", f"dependency {dep} ({st['operation']}) CANCELLED"
        if st["status"] == "SUCCEEDED":
            if not st.get("result_ref"):
                return "failed", f"dependency {dep} ({st['operation']}) succeeded without a result_ref: not runnable"
            results[dep] = {
                "operation": st[
                    "operation"
                ],  # the delivering operation (a finalizer or export retry when one delivered)
                "producer": st.get("producer") or st["operation"],
                "result_ref": st["result_ref"],
                "attempt_id": st.get("attempt_id"),  # the delivering attempt
                "epoch": st.get("epoch"),
            }
        elif st["status"] is None:
            waiting.append(f"{dep} not submitted")
        elif st["status"] == "FAILED":
            waiting.append(f"{dep} FAILED ({st['operation']}: :resume it, or :cancel the root)")
        else:
            waiting.append(f"{dep} {st['status']}")
    if waiting:
        return "waiting", "; ".join(waiting)
    return "ready", results


# ---- the gate -------------------------------------------------------------------------------------------------------
def _cancel_stage(ns: str, job: dict, why: str) -> None:
    """A waiting (never started) stage ends: its attempt is marked cancelled (a late start is refused), the Job
    deleted, the member settled at zero with the reason on record. Idempotent: a second pass finds nothing."""
    name = job["metadata"]["name"]
    try:
        attempts.mark_cancelled(name)
    except Exception as e:  # noqa: BLE001
        log.warning("stage %s: mark cancelled: %s", name, e)
    try:
        cancelled = jobs.cancel(ns, name, REGION)
    except HTTPException as e:
        if e.status_code != 404:
            raise
        cancelled = {"metadata": {"name": name, "annotations": {f"{LABEL}/cancelled": "deleted"}}}
    settle_deleted(cancelled)
    try:
        keys.note_member(name, why)
    except Exception as e:  # noqa: BLE001
        log.warning("stage %s: note: %s", name, e)
    log.info("stage %s/%s cancelled: %s", ns, name, why)


def _admit_stage(ns: str, job: dict, results: dict) -> None:
    """Deliver the stage's private binding (its credentials are consumed now, not earlier), then one patch: the
    results into the pod template, the queue label on, the waiting label off. A crash between the two is safe: the
    delivery is idempotent and the next pass patches."""
    name, md = job["metadata"]["name"], job["metadata"]
    ann = md.get("annotations") or {}
    ex = json.loads(ann.get(f"{LABEL}/execution") or "null")
    execution.deliver(
        ex,
        name,
        name,
        ns,
        md["labels"][f"{LABEL}/tenant"],
        jobs.placement_regions(job) or [REGION],
        owner=jobs.operation_owner(ann, name),
    )
    profile = md["labels"].get(f"{LABEL}/profile")
    if not profile:
        raise HTTPException(500, f"stage {name}: no profile label: cannot name its Kueue queue")
    body = {
        "metadata": {
            "labels": {QUEUE_LABEL: profile, WAITING: None},
            "annotations": {ADMITTED_AT: iso(datetime.now(timezone.utc)), WAIT_REASON: None},
        },
        "spec": {"template": {"metadata": {"annotations": {RESULTS: json.dumps(results, sort_keys=True)}}}},
    }
    body["metadata"]["annotations"][RESULTS] = json.dumps(results, sort_keys=True)  # continuations inherit it
    retry(kube.batch().patch_namespaced_job, name, ns, body)
    log.info("stage %s/%s admitted: %s", ns, name, sorted(results))


def _note_waiting(ns: str, job: dict, why: str) -> None:
    if (job["metadata"].get("annotations") or {}).get(WAIT_REASON) == why:
        return
    try:
        retry(
            kube.batch().patch_namespaced_job,
            job["metadata"]["name"],
            ns,
            {"metadata": {"annotations": {WAIT_REASON: why}}},
        )
    except Exception as e:  # noqa: BLE001
        log.warning("stage %s: reason: %s", job["metadata"]["name"], e)


def _stage_jobs(ns: str, root: str) -> list[dict]:
    b = kube.batch()
    return [
        b.api_client.sanitize_for_serialization(j)
        for j in retry(b.list_namespaced_job, ns, label_selector=f"{STAGE_ROOT}={root}").items
    ]


def cancel_members(ns: str, root: str, why: str) -> list[str]:
    """Every active stage member of a root (the stages and their continuations, from the ledger) is cancelled: the
    root's cancel fans out, the gate's deadline path does the same. Waiting ones are deleted and settled at zero,
    running ones stopped as any run; a member whose Job is already gone is settled by the reconciler's sweep.
    Returns the names it cancelled."""
    out = []
    for m in keys.run_members(root):
        if m.get("kind") != "stage" or m["state"] != "active":
            continue
        name = m["operation"]
        try:
            job, _region = jobs.find(ns, name)
        except HTTPException as e:
            if e.status_code != 404:
                raise
            continue
        if status.finished(job) or (job["metadata"].get("annotations") or {}).get(f"{LABEL}/cancelled"):
            continue
        if is_waiting(job):
            _cancel_stage(ns, job, why)
        else:
            attempts.mark_cancelled(name)
            settle_deleted(jobs.cancel(ns, name, REGION))
            keys.note_member(name, why)
        out.append(name)
    return out


def _close(ns: str, root: str, why: str) -> list[str]:
    """Cancel what still waits or runs of the graph and close it: `<stage>:cancelled`..., `<root>:closed`."""
    out = [f"{n}:cancelled" for n in cancel_members(ns, root, why)]
    keys.cancel_graph(root, why)
    return out + [f"{root}:closed"]


def gate() -> list[str]:
    """One pass over the open graphs (the reconcile loop, every 15 s, each API replica; every step is idempotent):
    a waiting stage whose dependencies all delivered is admitted; one with a failed dependency is cancelled; a
    cancelled graph, a failed root or a passed deadline cancels what waits and closes the graph (nothing that never
    was submitted will be). Returns `<stage>:admitted|cancelled` and `<root>:closed` for what changed."""
    out: list[str] = []
    try:
        graphs = keys.open_graphs()
    except Exception as e:  # noqa: BLE001
        log.warning("stage gate: graphs unavailable: %s", e)
        return out
    now = datetime.now(timezone.utc)
    for g in graphs:
        root, ns = g["root"], f"{cfg.TENANT_NS_PREFIX}{g['tenant']}"
        try:
            out.extend(_gate_graph(ns, g, now))
        except Exception:  # noqa: BLE001
            log.exception("stage gate: graph of %s", root)
    return out


def _gate_graph(ns: str, g: dict, now: datetime) -> list[str]:
    root, out, memo = g["root"], [], {}
    root_state = state_of(ns, keys.run_head(root) or root, memo)
    if root_state["status"] == "CANCELLED":
        return _close(ns, root, "root CANCELLED")
    if g.get("deadline_at") and g["deadline_at"] <= now:
        members = {m["stage"] for m in keys.run_members(root) if m.get("kind") == "stage"}
        missing = sorted(s for s in g["stages"] if s not in members)
        return _close(
            ns, root, "graph deadline passed" + (f"; never submitted: {', '.join(missing)}" if missing else "")
        )
    failed_dep = False
    for job in _stage_jobs(ns, root):
        if not is_waiting(job):
            continue
        st = stage_of(job) or {}
        name = job["metadata"]["name"]
        deps = st.get("depends_on") or dependencies(g, st.get("id") or "")
        verdict, detail = evaluate(ns, g, deps, memo)
        if verdict == "failed":
            _cancel_stage(ns, job, str(detail))
            out.append(f"{name}:cancelled")
        elif verdict == "ready":
            try:
                _admit_stage(ns, job, detail)  # type: ignore[arg-type]
                out.append(f"{name}:admitted")
            except HTTPException as e:
                # the stage's own binding is not there (yet), or claimed by another operation: it keeps waiting,
                # with the reason on the handle, until the graph's deadline
                _note_waiting(ns, job, f"not admitted: {e.detail}"[:300])
        else:
            _note_waiting(ns, job, f"waiting for {detail}"[:300])
            if "FAILED (" in str(detail):
                failed_dep = True
    if not out and (failed_dep or root_state.get("status") == "FAILED"):
        out += _settle_failure(ns, g, root_state, now, memo)
    return out


def _settle_failure(ns: str, g: dict, root_state: dict, now: datetime, memo: dict) -> list[str]:
    """Failure settlement (docs/JOBS.md "Stage composition"): an open graph that is the owner's to resume or cancel
    (`action_required`: nothing active, a lineage head FAILED, the rest parked on it) and has been so for
    GRAPH_FAILURE_GRACE_S after the newest failure is closed by the gate as the deadline closes it: the parked
    stages are retired (never started: deleted, settled at zero, the reason on record), the graph is cancelled with
    the reason, and the root's slot is released once nothing is active. The failed head's Job, volume, attempts and
    charges are untouched; its lineage may still be continued afterwards (a `:resume` re-acquires the slot). Before
    this (0.11.110) a root that failed on its own kept its slot and its never-launched stages active until the
    graph's deadline, days later (boltzgen op-b60c9ff341028d80, 2026-10-14). A failure whose time is unknown waits
    for the deadline."""
    root = g["root"]
    view = graph_view(ns, g, root_state=root_state, memo=memo)
    if not view.get("action_required"):
        return []
    failed_at = [b["failed_at"] for b in view["blocked_on"] if b.get("failed_at")]
    if len(failed_at) < len(view["blocked_on"]):
        return []
    newest = max(parse_ts(t) for t in failed_at)
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    if newest + timedelta(seconds=cfg.GRAPH_FAILURE_GRACE_S) > now:
        return []
    heads = ", ".join(f"{b['stage']} FAILED ({b['operation']})" for b in view["blocked_on"])
    return _close(
        ns, root, f"{heads}; not resumed within {cfg.GRAPH_FAILURE_GRACE_S} s of the failure: graph closed"[:300]
    )


# ---- the root's view ------------------------------------------------------------------------------------------
def graph_view(ns: str, g: dict, root_state: dict | None = None, memo: dict | None = None) -> dict:
    """The root handle's `graph` (docs/API.md "Operations"): every declared stage with its dependencies, operation,
    status, attempt and reason; the graph's own status: PENDING while a stage is not settled, SUCCEEDED when every
    stage succeeded, FAILED when one failed or was cancelled or the graph was closed before every stage ran,
    CANCELLED when the root was cancelled. `root_state` (state_of the root's lineage head; read here when not given)
    lets a root that FAILED on its own count as a failed head of the graph (`action_required`, `blocked_on`)."""
    memo = {} if memo is None else memo
    if root_state is None:
        root_state = state_of(ns, keys.run_head(g["root"]) or g["root"], memo)
    lineages: dict[str, list[dict]] = {}
    for m in keys.run_members(g["root"]):
        if m.get("kind") == "stage":
            lineages.setdefault(m["stage"], []).append(m)  # oldest first: the last one is the lineage head
    stages, statuses = [], []
    for sid, deps in (g.get("stages") or {}).items():
        lineage = lineages.get(sid) or []
        entry = {"id": sid, "depends_on": list(deps) or [ROOT], "operation": None, "status": None}
        if lineage:
            m = lineage[-1]
            st = state_of(ns, m["operation"], memo)
            status = st.get("status") or ("CANCELLED" if m["state"] != "active" else "QUEUED")
            entry.update(
                {
                    "operation": m["operation"],
                    "status": status,
                    "attempt_id": st.get("attempt_id"),
                    "epoch": st.get("epoch"),
                    "delivered_by": st["operation"] if st.get("operation") != m["operation"] else None,
                    "result_ref": st.get("result_ref"),
                    "reason": m.get("note") or lineage[0].get("note"),
                    **({"continuations": len(lineage) - 1} if len(lineage) > 1 else {}),
                    **({"_ended_at": st["ended_at"]} if status == "FAILED" and st.get("ended_at") else {}),
                }
            )
            if status == "QUEUED":
                # a QUEUED member is either admitted and waiting for a node (active: it will run) or dependency-gated
                # (parked by the gate until its dependencies deliver: it cannot mask a failed dependency)
                entry["gated"] = _gated(ns, m["operation"], memo)
        else:
            entry["status"] = "NOT_SUBMITTED"
        stages.append(entry)
        statuses.append(entry["status"])
    if statuses and all(s == "SUCCEEDED" for s in statuses):
        overall = "SUCCEEDED"
    elif g.get("cancelled_at") and (g.get("reason") or "").startswith("cancelled by"):
        overall = "CANCELLED"
    elif g.get("cancelled_at") or g.get("closed_at"):
        overall = "FAILED"  # over, and not every stage delivered (a resumed stage that delivers later turns it)
    else:
        overall = "PENDING"  # open: a FAILED stage may be resumed, a cancelled one cancels its dependents
    out = {
        "status": overall,
        "stages": stages,
        "deadline_at": iso(g.get("deadline_at")),
        "cancelled": bool(g.get("cancelled_at")),
        **({"reason": g["reason"]} if g.get("reason") else {}),
    }
    out.update(_action_required(g["root"], stages, overall, root_state))
    for s in stages:
        s.pop("_ended_at", None)
    return out


ACTIVE = (
    "RUNNING",
    "FINALIZING",
    "EXPORT_FAILED",
)  # a member that will still change by itself (or by a delivery retry)


def _gated(ns: str, operation: str, memo: dict | None = None) -> bool:
    """A QUEUED member parked by the gate (label stage-waiting), as opposed to one admitted and waiting for a node."""
    try:
        job, _r = _find(ns, operation, memo)
    except HTTPException:
        return False
    return is_waiting(job)


def _action_required(root: str, stages: list[dict], overall: str, root_state: dict | None = None) -> dict:
    """The owner-facing signal (docs/API.md "Operations"): an open graph in which nothing is active any more and at
    least one stage FAILED will not change without the owner (`:resume` the failed head, or `:cancel` the root).
    Projection only: no state is touched, nothing is failed or retried, the graph's deadline still closes it. A
    dependency-gated QUEUED member is not active (a parked collector cannot mask its failed dependency); an admitted
    QUEUED, RUNNING, FINALIZING or delivery-retryable sibling is, and suppresses the signal while it may still deliver.
    `blocked_on` carries bounded metadata only: the failed lineage head, its attempt, the actions and the direct
    dependents parked on it. The root's own run is a head too (`stage` "root"): a root that FAILED before its stages
    ran blocks every stage that depends on it."""
    failed = [s for s in stages if s["status"] == "FAILED"]
    if root_state and root_state.get("status") == "FAILED" and overall == "PENDING":
        failed.insert(
            0,
            {
                "id": ROOT,
                "operation": root_state.get("operation"),
                "status": "FAILED",
                "attempt_id": root_state.get("attempt_id"),
                **({"_ended_at": root_state["ended_at"]} if root_state.get("ended_at") else {}),
            },
        )
    active = [s for s in stages if s["status"] in ACTIVE or (s["status"] == "QUEUED" and not s.get("gated", False))]
    waiting = [s["id"] for s in stages if s["status"] == "QUEUED" and s.get("gated")]
    blocked = [
        {
            "stage": s["id"],
            "operation": s["operation"],
            "status": s["status"],
            "attempt_id": s.get("attempt_id"),
            "actions": [f":resume {s['operation']}", f":cancel {root}"],
            "dependents": [d["id"] for d in stages if s["id"] in (d.get("depends_on") or [])],
            **({"failed_at": s["_ended_at"]} if s.get("_ended_at") else {}),
        }
        for s in failed
    ]
    required = overall == "PENDING" and bool(failed) and not active
    return {"action_required": required, "blocked_on": blocked, "waiting": waiting}


def apply_to_root(op: dict, view: dict) -> None:
    """The root is terminal only when its stages are (docs/JOBS.md "Stage composition"): a root whose own run
    SUCCEEDED reports RUNNING (phase `stages`) while the graph is PENDING, and the graph's outcome once it is over;
    a root that FAILED or was CANCELLED on its own keeps that."""
    op["graph"] = view
    if op.get("status") != "SUCCEEDED":
        return
    if view["status"] == "PENDING" and view.get("action_required"):
        # nothing will change without the owner: say so on the handle itself, status RUNNING (resumable), no auto-fail
        op["status"], op["phase"] = "RUNNING", "action_required"
        heads = "; ".join(f"stage {b['stage']} FAILED ({b['operation']})" for b in view.get("blocked_on") or [])
        waiting = ", ".join(view.get("waiting") or []) or "none"
        op["reason"] = f"{heads}: resume it or cancel the root; waiting: {waiting}"[:300]
    elif view["status"] == "PENDING":
        op["status"], op["phase"] = "RUNNING", "stages"
        pending = [s["id"] for s in view["stages"] if s["status"] not in ("SUCCEEDED",)]
        op["reason"] = f"waiting for stages: {', '.join(pending)}"[:300]
    elif view["status"] in ("FAILED", "CANCELLED"):
        op["status"] = view["status"]
        failed = [f"{s['id']} {s['status']}" for s in view["stages"] if s["status"] != "SUCCEEDED"]
        op["error"] = (view.get("reason") or "; ".join(failed))[:300]


# ---- the run ledger: admission, continuation and the graph's cancellation ---------------------------------------
def admit(
    p: Principal, operation: str, gpu_seconds: float = 0.0, counts_request: bool = True, run_member: bool = False
) -> dict:
    """Reserve the slot, the request and the GPU seconds of `operation` on the caller's key (keys.admit); nothing
    without the database, nothing for the router key (it carries no budget). `run_member`: the operation is the root
    of a logical run whose continuations join its reservation (keys.prepare_run_member)."""
    if not db.enabled() or p.role == keys.ROUTER["role"]:
        return {"replay": False}
    try:
        return keys.admit(keys.key_hash(p.key), operation, p.tenant, gpu_seconds, counts_request, run_member)
    except keys.KeyLimit as e:
        # An endpoint may itself answer429 AFTER admission. Only this ledger
        # refusal proves that no execution was admitted by this request.
        headers = {"X-Serverless2-Admission": "refused"} if e.status == 429 else None
        raise HTTPException(e.status, e.detail, headers=headers)


def continuation_admission(
    root: str, predecessor: str | None, transfer_only: bool = False, stage: str | None = None, kind: str = "stage"
):
    """The admission a continuation builder calls with its rendered Job, right before creating it: the member joins
    the run's reservation on the root's original key (keys.prepare_run_member: key still valid, current head, budget
    for its GPU bound, the slot reused or re-acquired). The returned callable raises HTTPException with the ledger's
    status."""

    def admit(job: dict) -> None:
        try:
            keys.prepare_run_member(
                root,
                job["metadata"]["name"],
                predecessor,
                jobs.gpus_of(job),
                jobs.deadline_of(job),
                transfer_only,
                **({"kind": kind, "stage": stage} if stage else {}),
            )
        except keys.KeyLimit as e:
            raise HTTPException(e.status, e.detail)

    return admit


def require_current(p: Principal, source: str, root: str) -> None:
    """A public continuation renders from `source` (the Job the caller named), while the ledger admits it after the
    lineage's current head. The two must be the same operation, or the head an export retry / finalizer of the source
    (`export-of` = source: the compute attempt stays where it was): a resume or export of an older member after a
    newer compute successor exists is refused with the head named, never a silent reopening of old state."""
    if not db.enabled():
        return
    member = keys.member_of(root)
    if member is None:
        return
    root, _stage, _kind, head = _lineage(member, root)
    if head in (None, source):
        return
    found = jobs.find_or_none(p.namespace, head)
    if found and (found[0]["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/export-of") == source:
        return
    if not found and _pending_admission(p.namespace, root, head):
        return  # a pending admission: continue_run admits only the same intent (the operation named by the head)
    raise HTTPException(
        409,
        f"operation {source} was continued by {head}; continue {head} (the current member of its lineage), not {source}",
    )


def continue_run(root: str, build, transfer_only: bool = False, ns: str | None = None):
    """Run a continuation builder (jobs.resume/export_again/switch_class/recover, given as a callable taking the
    admission callback) under the run's ledger: the new member follows the current head. Nothing is ever retired
    automatically: a head that was admitted but whose Job exists nowhere and that no attempt reported (an API process
    that died between its admission and its create, a create whose answer was lost, or a replica paused between the
    two: indistinguishable from outside) stays PENDING, and the lineage continues only by the same intent — the
    builder that renders that very operation name (the deterministic `-x<n>` / `-r<n>` of a replayed request) is
    admitted as a replay of the pending member and creates its Job (AlreadyExists is the same Job); any other
    continuation is refused (409 naming the pending admission). A builder that fails after admission is never rolled
    back on an immediate absence either (`_reconcile_continuation`): only a definite refusal (4xx) rolls the member
    back. `ns` (the tenant namespace) enables the pending-head judgement; without it the head is taken as is."""
    if not db.enabled():
        return build(None)
    root, stage, kind, head = _lineage(keys.member_of(root), root)
    if head is None:
        return build(None)
    pending_head = head if ns is not None and head != root and _pending_admission(ns, root, head) else None
    predecessor = keys.member_of(head)["predecessor"] if pending_head else head
    pending: dict = {}

    def admit(job: dict) -> None:
        name = job["metadata"]["name"]
        if pending_head and name != pending_head:
            raise HTTPException(
                409,
                f"operation {root}: its lineage has a pending admission {pending_head} (admitted, its Job not created or "
                "not readable yet); only a replay of the same request continues it, nothing else is admitted meanwhile",
            )
        continuation_admission(root, predecessor, transfer_only, stage=stage, kind=kind)(job)
        pending["name"] = name

    try:
        return build(admit)
    except Exception as e:
        if pending.get("name") and pending["name"] != pending_head:
            found = _reconcile_continuation(pending["name"], ns, e)
            if found is not None:
                return found, True  # created; the answer was lost: the member stays, the Job is the continuation
        raise


def _lineage(member: dict | None, root: str) -> tuple[str, str | None, str, str | None]:
    """(root, stage, kind, head) of the lineage a continuation of `root` follows on the ledger. A stage of a graph
    (docs/JOBS.md "Stage composition") follows the stage's own head inside the graph root's run (the root's key, slot
    and budget), never the root's lineage or a sibling's; an endpoint child (docs/JOBS.md "Endpoint children") its
    own head (its unit's lineage) inside the parent run's ledger, nothing inferred or counted again; anything else
    the run's head."""
    if member and member["kind"] == "stage":
        return member["root"], member["stage"], "stage", keys.stage_head(member["root"], member["stage"])
    if member and member["kind"] == "child":
        return member["root"], member["stage"], "child", keys.child_head(member["root"], member["stage"])
    return root, None, "stage", keys.run_head(root)


def _pending_admission(ns: str, root: str, head: str) -> bool:
    """Whether the lineage head is an admission without a Job: active member, no Job anywhere (a clean 404 across the
    regions; an unreachable cluster is not an absence), no attempt record (a Job that ran and expired has records). Such a
    head is never retired here (another replica may be creating it right now); it is resolved by the same intent."""
    try:
        if jobs.find_or_none(ns, head) is not None:
            return False
    except HTTPException:
        return False
    if attempts.enabled() and any(r["operation"] == head for r in attempts.for_operation(root)):
        return False
    m = keys.member_of(head)
    return bool(m) and m["state"] == "active" and (m["predecessor"] is not None or m["kind"] == "child")


def _reconcile_continuation(name: str, ns: str | None, e: Exception):
    """After a builder raised past the admission: the member is NEVER rolled back here. A definite refusal (4xx)
    proves only that THIS caller's create was refused, not that no other replica created the operation (both may have
    admitted the same intent: one member, two creates); an ambiguous failure (5xx, transport) may still be accepted by
    the API server. So the member stays a pending admission in every case: the Job found is returned (the
    continuation exists, whoever created it); otherwise the same request replays the intent (continue_run admits it as
    a replay of the member and creates the Job). Returns the Job when it exists, else None."""
    if ns is not None:
        try:
            found = jobs.find_or_none(ns, name)
        except HTTPException as f:
            log.warning(
                "continuation %s: create failed (%s), cluster unreadable (%s): member kept", name, str(e)[:80], f.detail
            )
            return None
        if found is not None:
            log.warning(
                "continuation %s: its Job exists although this create failed (%s); member kept", name, str(e)[:120]
            )
            return found[0]
    log.warning(
        "continuation %s: create failed (%s): member kept as a pending admission (the same request replays it)",
        name,
        str(e)[:120],
    )
    return None


def settle_deleted(job: dict) -> None:
    """A run cancelled before it ever started (jobs.cancel deleted it): its member measured nothing and held no GPU
    (never-started is the evidence, not an absent pod): settled at zero, the run closes when nothing else is active."""
    if not db.enabled() or (job["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/cancelled") != "deleted":
        return
    try:
        keys.settle_run_member(job["metadata"]["name"], 0.0)
    except Exception as e:  # noqa: BLE001 - the reconciler's sweep settles what this missed
        log.warning("settlement of cancelled %s: %s", job["metadata"]["name"], e)


def release(operation: str, gpu_seconds: float | None = None) -> None:
    if db.enabled():
        try:
            keys.release(operation, gpu_seconds)
        except Exception as e:  # noqa: BLE001 - the reconciler's sweep releases what this missed
            log.warning("release of %s: %s", operation, e)


def _gpu_reservation(gpus: int, req, p: Principal) -> float:
    """GPU seconds a run may take: its GPUs times its enforced timeout, for a key with a finite GPU-second budget
    (such a key must give timeout_s on a GPU run); unbounded keys and CPU-only runs reserve nothing."""
    if p.info.get("budget_gpu_seconds") is None or gpus == 0:
        return 0.0
    if not req.timeout_s:
        raise HTTPException(400, "this key has a GPU-second budget: give timeout_s so the run's reservation is bounded")
    return float(gpus * req.timeout_s)


def create_admitted(p, name, job, region, secret, req, mode, model: str, stage: dict | None = None):
    """The operation's owner on record (keys.own_operation), then its admission on the caller's key from the rendered
    Job (its real GPU count: a slot, a request and, for a key with a GPU-second budget, GPUs x the run's timeout,
    released with the measured value when the attempt is billed) and its creation, under the per-operation lock: a
    refund cannot race a replay. Returns (job, created)."""
    if db.enabled():
        try:
            keys.own_operation(name, p.tenant, p.owner, model)
        except keys.KeyLimit as e:
            raise HTTPException(e.status, e.detail)
    with keys.operation_lock(name) if db.enabled() else nullcontext():
        return _create_locked(p, name, job, region, secret, req, mode, stage)


def _create_locked(p, name, job, region, secret, req, mode, stage: dict | None):
    # Existing Jobs already own their admission, including terminal replays.
    existing = jobs.find_or_none(p.namespace, name, region)
    if existing:
        check_operation_owner(p, existing[0])
        reservation = {"replay": True}
    elif stage:
        # a declared stage joins its root's run on the root's key (keys.prepare_run_member kind=stage: the key still
        # valid, its GPU bound within the budget next to its siblings, the root's slot shared; no head rule)
        try:
            reservation = keys.prepare_run_member(
                stage["root"], name, None, jobs.gpus_of(job), jobs.deadline_of(job), kind="stage", stage=stage["id"]
            )
        except keys.KeyLimit as e:
            raise HTTPException(e.status, e.detail)
    else:
        reservation = admit(
            p,
            name,
            _gpu_reservation(jobs.gpus_of(job), req, p) if mode == "run" else 0.0,
            run_member=mode in ("run", "async"),  # a run or an endpoint call: its continuations join this reservation
        )
    ex = json.loads((job["metadata"].get("annotations") or {}).get(f"{cfg.LABEL}/execution") or "null") or {}
    if db.enabled() and ex.get("graph") and not reservation.get("replay"):
        # the root's stages are sealed with its admission (docs/JOBS.md "Stage composition"); the run stays open
        # (slot, request) until every declared stage settled or the graph is cancelled
        deadline = datetime.now(timezone.utc) + timedelta(
            seconds=int(ex["graph"].get("deadline_s") or DEFAULT_DEADLINE_S)
        )
        keys.declare_graph(name, p.tenant, p.owner, ex["graph"]["stages"], deadline, ex["graph"].get("models"))
    try:
        created_job, created = jobs.create(p.namespace, job, region, secret)
    except Exception:
        if db.enabled() and not reservation.get("replay"):
            # A timeout can mean the create succeeded. Only a confirmed absence
            # permits a refund; ambiguous failures retain the reservation.
            try:
                if jobs.find_or_none(p.namespace, name, region) is None:
                    keys.rollback_run_member(name) if stage else keys.abandon(name, keys.key_hash(p.key))
            except Exception:
                pass
        raise
    check_operation_owner(p, created_job)
    return created_job, created


def require_delegation(p: Principal, model: str, root: str, stage: str) -> None:
    """A stage on a model the caller's key is not granted is allowed only when every one of these holds: the root's graph
    exists in this tenant and belongs to this owner, is open, and declared this stage bound to exactly this model; the
    root's run class (operator-owned) lists this model among its `delegates`; the root holds a live reservation on the
    caller's key (the same key, still admitted). Nothing is granted beyond that graph."""
    if not db.enabled():
        raise HTTPException(403, f"model {model} is not granted to this key")
    g = keys.graph_of(root)
    if g is None or g["tenant"] != p.tenant or g["owner"] != p.owner:
        raise HTTPException(403, f"model {model} is not granted to this key (no delegating root {root} of yours)")
    if g.get("cancelled_at") or g.get("closed_at"):
        raise HTTPException(403, f"model {model} is not granted to this key (the graph of {root} is over)")
    if (g.get("models") or {}).get(stage) != model:
        raise HTTPException(
            403, f"model {model} is not granted to this key (stage {stage!r} of {root} is not declared on it)"
        )
    record = keys.operation_owner(root)
    root_model = catalog.require(record[2]) if record else None
    if not root_model or model not in (root_model.get("delegates") or []):
        raise HTTPException(403, f"model {model} is not granted to this key (the root's class does not delegate to it)")
    if keys.reservation_key(root) != keys.key_hash(p.key):
        raise HTTPException(403, f"model {model} is not granted to this key (root {root} is not active on this key)")


def stage_request(
    ex: dict | None, mode: str, p: Principal, idempotency_key: str | None
) -> tuple[dict | None, dict | None]:
    """A submission that is a declared stage of a root's graph (docs/JOBS.md "Stage composition"): the root's graph
    must exist in this tenant, belong to the same owner, declare the stage, and still be open. Returns (stage, graph)
    or (None, None) for an ordinary submission."""
    stage = (ex or {}).get("stage")
    if not stage:
        return None, None
    if mode != "run":
        raise HTTPException(400, "execution.stage: a stage is a run (not an endpoint call)")
    if not db.enabled():
        raise HTTPException(409, "stage composition needs the control API's ledger (no database here)")
    if idempotency_key:
        raise HTTPException(400, "a stage's identity is its root and id: no Idempotency-Key")
    g = keys.graph_of(stage["root"])
    if g is None or g["tenant"] != p.tenant:
        raise HTTPException(404, f"operation {stage['root']}: no stage graph (declare execution.graph on the root)")
    if g["owner"] != p.owner:
        raise HTTPException(403, f"operation {stage['root']}: the stages of a graph belong to its root's owner")
    if stage["id"] not in (g["stages"] or {}):
        raise HTTPException(400, f"stage {stage['id']!r} is not declared by {stage['root']} ({sorted(g['stages'])})")
    if g.get("cancelled_at"):
        raise HTTPException(409, f"the graph of {stage['root']} is cancelled: no stage attaches")
    # a closed graph (every stage settled): a stage that ran answers with its durable head (_settled_stage); one that
    # never was submitted cannot attach any more (409 below, after the lookup)
    return stage, g


def cancel_run(ns: str, job: dict, current: str, root: str) -> dict:
    """`:cancel` of a live operation (`job`, `current` its name: the operation named, or the active finalizer of the
    root's handle): the attempt is marked (a late "succeeded" is refused from now on), the Job stopped, the root's
    endpoint children cancelled with it, a never-started member settled at zero, an attached native worker asked to
    stop; a graph root takes its stages and its lineage head with it (docs/JOBS.md "Stage composition"). Returns the
    operation as cancelled."""
    attempts.mark_cancelled(current)
    cancelled = jobs.cancel(ns, current, jobs.home_of(job))
    try:  # endpoint children of the root go with it (docs/JOBS.md "Endpoint children"); the sweep repeats it durably
        import children

        children.cancel_for_root(ns, root)
    except Exception as e:  # noqa: BLE001 - the sweep cancels what this missed
        log.warning("child cascade of %s: %s", root, e)
    settle_deleted(cancelled)
    op = jobs.normalise(cancelled)
    if op["status"] in ("QUEUED", "RUNNING"):
        op["status"] = "CANCELLED"  # the Job controller converges within seconds (pods get SIGTERM)
    if db.enabled() and (job["metadata"].get("labels") or {}).get(f"{LABEL}/mode") == "async":
        import leases

        leases.request_cancel(current, "cancelled by the owner")  # a request to the attached worker, never a release
    if db.enabled() and (g := keys.graph_of(root)) and not g.get("cancelled_at"):
        # the root of a graph: every stage goes with it, and so does the root lineage's current head (a resumed root
        # that is still queued or running); nothing attaches any more
        names = cancel_graph_members(ns, root, current)
        op["graph"] = graph_view(ns, keys.graph_of(root))
        op["status"] = "CANCELLED"
        log.info("cancel %s: stages cancelled: %s", root, names)
    return op


def cancel_graph_members(ns: str, root: str, current: str) -> list[str]:
    """The graph's cancellation: its stage members, and the root lineage's head when that is another, still active
    operation (a manual continuation of the root): marked, stopped, settled like any cancel. The graph is closed."""
    why = f"cancelled by the caller with root {root}"
    names = cancel_members(ns, root, why)
    head = keys.run_head(root)
    if head and head != current:
        found = jobs.find_or_none(ns, head)
        if found is not None and not status.finished(found[0]):
            attempts.mark_cancelled(head)
            settle_deleted(jobs.cancel(ns, head, jobs.home_of(found[0])))
            names.append(head)
    keys.cancel_graph(root, why)
    return names


def cancel_expired_root(ns: str, id: str, synthetic: dict) -> dict:
    """`:cancel` on an operation whose Job expired: a graph root still cancels its graph (the stages and the root
    lineage's head); anything else is over."""
    root = (synthetic["metadata"].get("annotations") or {}).get(f"{LABEL}/resumed-from") or synthetic["metadata"][
        "name"
    ]
    if not (db.enabled() and (g := keys.graph_of(root))):
        raise HTTPException(409, f"operation {id} is over; nothing to cancel")
    op = dict(synthetic["_synthetic"])
    if not g.get("cancelled_at"):
        attempts.mark_cancelled(id)
        names = cancel_graph_members(ns, root, id)
        log.info("cancel %s (expired root): graph cancelled: %s", root, names)
    op["graph"] = graph_view(ns, keys.graph_of(root))
    op["status"] = "CANCELLED"
    return op
