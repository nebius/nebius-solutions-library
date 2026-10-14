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
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

import attempts
import config as cfg
import db
import execution
import jobs
import keys
import kube
import status
from config import REGION
from resilience import retry

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


def state_of(ns: str, operation: str, memo: dict | None = None) -> dict:
    """{"status", "result_ref", "attempt_id", "operation"} of an operation, as the gate sees it: the live Job
    (following fallback/recovery successors) with its durable attempt records, else the durable rows of an expired
    Job, else status None (nothing exists: not submitted yet). An export retry or finalizer that delivered after the
    Job failed counts (app._delivered)."""
    from app import _delivered  # app imports this module; resolved at call time

    tenant = ns[len(cfg.TENANT_NS_PREFIX) :] if ns.startswith(cfg.TENANT_NS_PREFIX) else ns
    current = operation
    for _ in range(16):
        try:
            job, _region = _find(ns, current, memo)
        except HTTPException as e:
            if e.status_code != 404:
                raise
            if not attempts.enabled():
                return {"operation": current, "status": None}
            nxt = attempts.successor_of(current) or ledger_head(ns, current)
            if nxt and nxt != current:
                current = nxt
                continue
            syn = attempts.synthesize(current, tenant)
            if syn is None:
                # no Job, no attempt row: either never submitted, or cancelled before it started (its Job deleted; the
                # cancel mark and the settled ledger member are what remains)
                marks = attempts._marks(current)
                member = keys.member_of(current)
                if (marks and marks[1]) or (member and member["state"] != "active"):
                    return {"operation": current, "status": "CANCELLED"}
                return {"operation": current, "status": None}
            op = dict(syn)
            if op.get("status") in ("EXPORT_FAILED", "FINALIZING", "FAILED"):
                root = op.get("resumed_from") or current
                op.update(_delivered(op, current, root))
            return _state(current, op)
        nxt = (job["metadata"].get("annotations") or {}).get(f"{LABEL}/successor") or ledger_head(ns, current)
        if nxt and nxt != current:
            current = nxt
            continue
        root = (job["metadata"].get("annotations") or {}).get(f"{LABEL}/resumed-from") or current
        rows = [r for r in attempts.for_operation(root) if r["operation"] == current] if attempts.enabled() else []
        op = jobs.normalise(job, None, rows)
        if attempts.enabled():
            op.update(attempts.lifecycle(current, root, rows))
            if op["status"] in ("EXPORT_FAILED", "FINALIZING", "FAILED"):
                op.update(_delivered(op, current, root))
        return _state(current, op)
    return {"operation": current, "status": None}


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
    from app import settle_deleted

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
            "annotations": {
                ADMITTED_AT: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                WAIT_REASON: None,
            },
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
    from app import settle_deleted

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
        why = "root CANCELLED"
        out += [f"{n}:cancelled" for n in cancel_members(ns, root, why)]
        keys.cancel_graph(root, why)
        return out + [f"{root}:closed"]
    if g.get("deadline_at") and g["deadline_at"] <= now:
        members = {m["stage"] for m in keys.run_members(root) if m.get("kind") == "stage"}
        missing = sorted(s for s in g["stages"] if s not in members)
        why = "graph deadline passed" + (f"; never submitted: {', '.join(missing)}" if missing else "")
        out += [f"{n}:cancelled" for n in cancel_members(ns, root, why)]
        keys.cancel_graph(root, why)
        return out + [f"{root}:closed"]
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
    newest = max(datetime.fromisoformat(t.replace("Z", "+00:00")) for t in failed_at)
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    if newest + timedelta(seconds=cfg.GRAPH_FAILURE_GRACE_S) > now:
        return []
    heads = ", ".join(f"{b['stage']} FAILED ({b['operation']})" for b in view["blocked_on"])
    why = f"{heads}; not resumed within {cfg.GRAPH_FAILURE_GRACE_S} s of the failure: graph closed"[:300]
    out = [f"{n}:cancelled" for n in cancel_members(ns, root, why)]
    keys.cancel_graph(root, why)
    return out + [f"{root}:closed"]


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
        "deadline_at": g["deadline_at"].strftime("%Y-%m-%dT%H:%M:%SZ") if g.get("deadline_at") else None,
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
