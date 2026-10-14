"""From a Job (or JobSet) as read from Kubernetes to the operation the API returns (docs/JOBS.md "Status mapping").

`normalise` is the one shape of an operation; `_attempts` lists one attempt per pod, with the uploader's
attempt records from the bucket standing in for pods that preemption deleted; `gpu_seconds` is what the
billing CronJob prices. The predicates (`is_jobset`, `is_manager_job`, `is_mirror`) tell the kinds apart."""

import json
import urllib.parse
from datetime import datetime, timezone

import kube
from config import GRAFANA_URLS, KUEUE_QUEUE, LABEL, MULTIKUEUE_MANAGED_BY, MULTIKUEUE_ORIGIN_LABEL, REGION


def is_jobset(job: dict) -> bool:
    return job.get("kind") == "JobSet"


def is_manager_job(job: dict) -> bool:
    return (job.get("spec") or {}).get("managedBy") == MULTIKUEUE_MANAGED_BY


def is_mirror(job: dict) -> bool:
    """The worker's copy of a manager Job (MultiKueue labels it with its origin): status and pods of the
    operation, never an operation of its own."""
    return MULTIKUEUE_ORIGIN_LABEL in (job.get("metadata", {}).get("labels") or {})


PHASE = {"Pending": "QUEUED", "Running": "RUNNING", "Succeeded": "SUCCEEDED", "Failed": "FAILED", "Unknown": "QUEUED"}
FINALIZING_GRACE_S = 600  # a completed run without a delivery record is FINALIZING this long, then EXPORT_FAILED


def _fails_job(job: dict, exit_code) -> bool:
    """Whether `exit_code` of `main` is one the Job's podFailurePolicy answers with FailJob (no more pod retries)."""
    if exit_code is None:
        return False
    for rule in ((job.get("spec") or {}).get("podFailurePolicy") or {}).get("rules") or []:
        codes = rule.get("onExitCodes") or {}
        if (
            rule.get("action") == "FailJob"
            and codes.get("containerName", "main") == "main"
            and codes.get("operator", "In") == "In"
            and int(exit_code) in [int(v) for v in codes.get("values") or []]
        ):
            return True
    return False


def _main_state(pod: dict) -> dict:
    for c in pod.get("status", {}).get("containerStatuses") or []:
        if c.get("name") == "main":
            return c.get("state") or {}
    return {}


def _gpus(pod: dict) -> int:
    for c in pod.get("spec", {}).get("containers", []):
        if c.get("name") == "main":
            return int(((c.get("resources") or {}).get("limits") or {}).get("nvidia.com/gpu", 0) or 0)
    return 0


# "started" is the record the uploader writes when main starts; it stays the only one when the node died
# without a shutdown: the attempt was preempted, its end is unknown, it bills nothing.
RECORD_STATUS = {"succeeded": "SUCCEEDED", "failed": "FAILED", "interrupted": "PREEMPTED", "started": "PREEMPTED"}


def _pod_digest(p: dict) -> str | None:
    for cs in (p.get("status") or {}).get("containerStatuses") or []:
        if cs.get("name") == "main" and cs.get("imageID"):
            return cs["imageID"]
    return None


JOBSET_NAME_LABEL = "jobset.sigs.k8s.io/jobset-name"
JOBSET_RESTART_LABEL = "jobset.sigs.k8s.io/restart-attempt"
JOB_INDEX_LABEL = "batch.kubernetes.io/job-completion-index"


def _pod_gang_fields(p: dict) -> dict:
    """{pod_uid, gang_rank, gang_restart} as the live pod states them (metadata.uid; the completion index and the JobSet
    restart-attempt for a JobSet pod, None for an ordinary pod)."""
    md = p.get("metadata") or {}
    labels, ann = md.get("labels") or {}, md.get("annotations") or {}
    out = {"pod_uid": md.get("uid"), "gang_rank": None, "gang_restart": None}
    if labels.get(JOBSET_NAME_LABEL) or ann.get(JOBSET_NAME_LABEL):
        rank = labels.get(JOB_INDEX_LABEL) or ann.get(JOB_INDEX_LABEL)
        restart = labels.get(JOBSET_RESTART_LABEL) or ann.get(JOBSET_RESTART_LABEL) or "0"
        try:
            out["gang_rank"] = int(rank) if rank is not None else None
            out["gang_restart"] = int(restart)
        except ValueError:
            pass
    return out


def _lifecycle_fields(r: dict) -> dict:
    """What an attempt record carries beyond the pod's state (the uploader's `export`, the database's `export_state`)."""
    out = {
        k: r.get(k)
        for k in (
            "result_ref",
            "checkpoint_ref",
            "image_digest",
            "epoch",
            "pod_uid",
            "gang_rank",
            "gang_restart",
            "resume_checkpoint_ref",
            "resume_source_epoch",
            "resume_source",
        )
        if r.get(k) is not None
    }
    export = r.get("export_state") or r.get("export")
    if export is not None:
        out["export_state"] = export
    return out


def _attempts(pods: list, records: list | None = None, name: str | None = None) -> list[dict]:
    """One attempt per pod, live pods first; attempt records from the bucket (uploader-written,
    docs/JOBS.md) stand in for pods that preemption or node loss deleted. `_gpus` is internal."""
    out, seen = [], set()
    for p in pods:
        st, phase = p.get("status", {}), p.get("status", {}).get("phase", "Pending")
        state = _main_state(p)
        term, run = state.get("terminated") or {}, state.get("running") or {}
        disrupted = any(
            c.get("type") == "DisruptionTarget" and c.get("status") == "True" for c in (st.get("conditions") or [])
        )
        if term:  # the attempt is `main`: the uploader may still be running after it
            # an OOM kill ends with 137 like a preemption's SIGKILL: the kubelet's reason tells them apart (an OOM is the application's)
            s = (
                "SUCCEEDED"
                if term.get("exitCode") == 0
                else (
                    "FAILED"
                    if term.get("reason") == "OOMKilled"
                    else "PREEMPTED"
                    if disrupted or term.get("exitCode") in (137, 143)
                    else "FAILED"
                )
            )
        elif run:
            s = "PREEMPTED" if p["metadata"].get("deletionTimestamp") else "RUNNING"
        else:
            s = (
                "PREEMPTED"
                if (phase == "Failed" and disrupted) or p["metadata"].get("deletionTimestamp")
                else PHASE.get(phase, "QUEUED")
            )
        reason = term.get("reason") if term else (st.get("reason") or None)
        if disrupted:
            reason = next((c.get("message") for c in st["conditions"] if c.get("type") == "DisruptionTarget"), reason)
        seen.add(p["metadata"]["name"])
        out.append(
            {
                "attempt_id": p["metadata"]["name"],
                "attempt_role": (p["metadata"].get("labels") or {}).get(f"{LABEL}/attempt-role") or "run",
                "started_at": term.get("startedAt") or run.get("startedAt") or st.get("startTime"),
                "ended_at": term.get("finishedAt"),
                "status": s,
                "node": p["spec"].get("nodeName"),
                "gpu_class": (p["metadata"].get("labels") or {}).get(f"{LABEL}/gpu-class") or None,
                "_pool": p["spec"].get("nodeSelector", {}).get(f"{LABEL}/pool"),
                "reason": reason if s != "SUCCEEDED" else None,
                "exit_code": term.get("exitCode"),
                "_gpus": _gpus(p),
                # the runtime digest the API observes on the pod (containerStatuses[main].imageID); the record's value,
                # when it has one, replaces it below
                "image_digest": _pod_digest(p),
                # observed on the live pod: its UID and, for a JobSet pod, its rank and incarnation (the labels the
                # Job and JobSet controllers stamp); the record's observation, when it has one, replaces them below
                **_pod_gang_fields(p),
            }
        )
    for r in records or []:
        if name and r.get("operation") not in (None, name):
            continue
        if r.get("pod") in seen:  # the live pod is the attempt; the record adds what only the uploader knows
            next(a for a in out if a.get("attempt_id") == r.get("pod")).update(_lifecycle_fields(r))
            continue
        out.append(
            {
                "started_at": r.get("started_at") or None,
                "ended_at": r.get("ended_at") or None,
                "attempt_role": r.get("role") or "run",
                "status": RECORD_STATUS.get(r.get("status"), "FAILED"),
                "node": r.get("node") or None,
                "gpu_class": r.get("gpu_class") or None,
                "_pool": r.get("pool"),
                "attempt_id": r.get("pod"),
                "reason": {
                    "interrupted": "pod deleted (preempted)",
                    "started": "node lost without warning (no end record)",
                }.get(r.get("status")),
                "exit_code": r.get("exit_code"),
                "_gpus": int(r.get("gpus") or 0),
                **_lifecycle_fields(r),
            }
        )
    out.sort(key=lambda a: a["started_at"] or "")
    for i, a in enumerate(out, 1):
        a["index"] = i
    return out


def _ts(s) -> datetime | None:
    """Timestamps come as RFC 3339 `...Z` (raw API objects) or `...+00:00` (the client's sanitized datetimes)."""
    if not s:
        return None
    try:
        d = s if isinstance(s, datetime) else datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _secs(a, b) -> float | None:
    ta, tb = _ts(a), _ts(b)
    return (tb - ta).total_seconds() if ta and tb else None


def logs_url(ns: str, name: str, region: str | None) -> str | None:
    g = GRAFANA_URLS.get(region or "")
    if not g:
        return None
    q = json.dumps(["now-7d", "now", "Loki", {"expr": f'{{namespace="{ns}", pod=~"{name}-.*"}}'}])
    return f"{g}/explore?orgId=1&left={urllib.parse.quote(q)}"


def measured(value) -> float | None:
    """An accounting annotation (`<LABEL>/gpu-seconds`, `<LABEL>/billed`) as a number, or None when it holds no
    measurement: absent, the literal `unknown` (billing.py writes it when a run ended without a trustworthy
    measurement, or a native settlement had none: the reservation stays unknown, never free), or anything that is not
    a finite non-negative number (a malformed value is unknown, never zero). Readers must not raise on a value the
    platform itself wrote: an operation with unknown usage stays readable."""
    if value is None:
        return None
    try:
        x = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if x != x or x in (float("inf"), float("-inf")) or x < 0:
        return None
    return x


def usage_of(ann: dict) -> str:
    """`measured` (a number on record), `unknown` (settled without one: the key's reservation is kept as unknown GPU
    time, docs/API.md "Keys"), `pending` (not settled yet)."""
    key = f"{LABEL}/gpu-seconds"
    if key not in ann:
        return "pending"
    return "measured" if measured(ann[key]) is not None else "unknown"


def _json(text: str | None):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def _waiting(job: dict, region: str | None) -> tuple[str, str | None]:
    """(phase, reason) of a QUEUED run (docs/API.md): `waiting_for_admission` while no cluster admitted it (Kueue's
    condition message, `_reason`), else `waiting_for_node` while its newest pod has no node (the scheduler's
    `PodScheduled` message: a pool scaling from zero, a full pool) or no pod exists yet, else `starting` (a container
    is waiting: image pull, the init containers). Bounded text, read from the pod the API already lists: no Events."""
    labels, ann = job.get("metadata", {}).get("labels") or {}, job.get("metadata", {}).get("annotations") or {}
    if labels.get(f"{LABEL}/stage-waiting") == "true":  # docs/JOBS.md "Stage composition": not handed to Kueue yet
        return "waiting_for_dependencies", ann.get(f"{LABEL}/stage-waiting-reason") or "waiting for its dependencies"
    if job.get("_reason") is not None or (is_manager_job(job) and not job.get("_cluster")):
        return "waiting_for_admission", job.get("_reason")
    pods = job.get("_pods") or []
    if not pods:
        return "waiting_for_node", f"placed on {region}, waiting for a node" if region else "placed, waiting for a node"
    p = pods[-1]
    st = p.get("status") or {}
    sched = next((c for c in (st.get("conditions") or []) if c.get("type") == "PodScheduled"), None)
    if sched and sched.get("status") != "True":
        return "waiting_for_node", str(sched.get("message") or sched.get("reason") or "no node yet")[:300]
    for c in (st.get("initContainerStatuses") or []) + (st.get("containerStatuses") or []):
        w = (c.get("state") or {}).get("waiting") or {}
        if w:
            return "starting", (
                f"{c.get('name')}: {w.get('reason') or 'waiting'}" + (f" ({w['message']})" if w.get("message") else "")
            )[:300]
    return "starting", None


def normalise(job: dict, price_per_call: float | None = None, records: list | None = None) -> dict:
    md, st, sp = job.get("metadata", {}), job.get("status", {}) or {}, job.get("spec", {})
    labels, ann = md.get("labels", {}), md.get("annotations", {})
    conds = {c.get("type"): c for c in (st.get("conditions") or []) if c.get("status") == "True"}
    attempts = [
        {k: v for k, v in a.items() if not k.startswith("_")}
        for a in _attempts(job.get("_pods", []), records, md.get("name"))
    ]
    error = None
    if ann.get(f"{LABEL}/successor") and not ann.get(f"{LABEL}/cancelled"):
        status = (
            "SUPERSEDED"  # fallback or recovery moved the run: the successor is the live operation (docs/SCHEDULING.md)
        )
    elif "Complete" in conds or "SuccessCriteriaMet" in conds:
        status = "SUCCEEDED"
        # delivery, not exit code: a verified export (managed I/O) or the application's finalization record
        # (application I/O: RESULT.json, docs/JOBS.md "Attempts and finalization") must be on record
        # the delivering attempt: the newest successful one that carries a delivery record (a multi-node run's rank 0
        # exports for every rank), else the newest successful one
        last = next(
            (a for a in reversed(attempts) if a["status"] == "SUCCEEDED" and a.get("export_state") is not None), None
        ) or next((a for a in reversed(attempts) if a["status"] == "SUCCEEDED"), None)
        execution = _json(ann.get(f"{LABEL}/execution")) or {}
        done_at = _ts(st.get("completionTime"))
        age = (datetime.now(timezone.utc) - done_at).total_seconds() if done_at else None
        if last and last.get("export_state") == "failed":
            status, error = (
                "EXPORT_FAILED",
                "export failed after a successful run; the volume is kept (POST /v1/operations/<id>:export retries the transfer)",
            )
        elif (
            last
            and execution.get("io_mode") == "application"
            and last.get("export_state") is not None
            and not last.get("result_ref")
        ):
            status, error = (
                "EXPORT_FAILED",
                "the run ended without a finalization record (RESULT.json): its results are not confirmed",
            )
        elif (
            labels.get(f"{LABEL}/mode") == "run"
            or (labels.get(f"{LABEL}/mode") == "async" and execution.get("io_mode") == "application")
        ) and (not last or last.get("export_state") is None):
            # exit 0 without a delivery record is not delivered: the uploader may still be exporting or posting
            # (FINALIZING), after the grace period the delivery is unconfirmed (docs/JOBS.md "Attempts and finalization")
            if age is not None and age > FINALIZING_GRACE_S:
                status, error = (
                    "EXPORT_FAILED",
                    "no delivery record for the successful run: its export or finalization was not confirmed (volume kept if the uploader could not acknowledge)",
                )
            else:
                status = "FINALIZING"
    elif "Failed" in conds or "FailureTarget" in conds:
        status = "CANCELLED" if ann.get(f"{LABEL}/cancelled") else "FAILED"
    elif ann.get(f"{LABEL}/cancelled"):
        status = "CANCELLED"
    elif attempts and attempts[-1]["status"] == "FAILED" and _fails_job(job, attempts[-1].get("exit_code")):
        # the main container ended with an exit code the Job's failure policy turns into FailJob: the Job fails as soon
        # as the pod ends (the uploader may still be posting the record); the operation is over now, not RUNNING
        status = "FAILED"
        error = f"main exited {attempts[-1].get('exit_code')}: {attempts[-1].get('reason') or 'the Job fails on this exit code'}"
    elif any(a["status"] == "RUNNING" for a in attempts) or any(
        (p.get("status") or {}).get("phase") == "Running" for p in job.get("_pods", [])
    ):
        status = "RUNNING"  # includes finalization: main ended, the uploader is still exporting
    else:
        status = "QUEUED"  # gated by Kueue, pending for a node, being placed, or between attempts after a preemption
    if ann.get(f"{LABEL}/cancelled") and attempts and attempts[-1]["status"] in ("PREEMPTED", "RUNNING", "QUEUED"):
        attempts[-1]["status"], attempts[-1]["reason"] = "CANCELLED", "cancelled"  # the attempt the cancel interrupted
    c = conds.get("Failed") or conds.get("FailureTarget") or {}
    if status == "FAILED":
        last = next((a for a in reversed(attempts) if a["status"] == "FAILED"), None)
        error = (last or {}).get("reason") or c.get("message") or c.get("reason")
    try:
        inp = json.loads(ann.get(f"{LABEL}/input", "{}"))
    except ValueError:
        inp = {}
    # the region label is the dispatcher's pin (a cluster id or a region name); the operation's `region` is always the Nebius region
    pinned = labels.get(f"{LABEL}/region")
    region = (
        (kube.cluster_region(pinned) if pinned else None)
        or job.get("_region")
        or (None if is_manager_job(job) else REGION)
    )
    phase, waiting = _waiting(job, region) if status == "QUEUED" else (None, None)
    if is_jobset(job):  # a JobSet reports no start/completion time of its own: the pods' are the run's
        st = dict(st)
        st["startTime"] = min((a["started_at"] for a in attempts if a["started_at"]), default=None)
        if status in ("SUCCEEDED", "FAILED", "CANCELLED"):
            st["completionTime"] = max((a["ended_at"] or "" for a in attempts), default=None) or None
    ended = st.get("completionTime") or (
        max((a["ended_at"] or "" for a in attempts), default=None) if status in ("FAILED", "CANCELLED") else None
    )
    return {
        "id": md["name"],
        "name": ann.get(f"{LABEL}/name"),
        "model": labels.get(f"{LABEL}/model"),
        "mode": labels.get(f"{LABEL}/mode"),
        "status": status,
        "region": region,
        "tenant": labels.get(f"{LABEL}/tenant"),
        "profile": labels.get(f"{LABEL}/profile", KUEUE_QUEUE),
        "priority": labels.get(f"{LABEL}/priority", "normal"),
        "created_at": md.get("creationTimestamp"),
        "started_at": st.get("startTime"),
        "ended_at": ended or None,
        "duration_s": _secs(st.get("startTime"), ended),
        "timeout_s": sp.get("activeDeadlineSeconds") if sp.get("activeDeadlineSeconds") != 1 else None,
        "attempts": attempts,
        "logs_url": logs_url(md.get("namespace", ""), md["name"], region),
        "error": error,
        "input": inp,
        "resumed_from": ann.get(f"{LABEL}/resumed-from"),
        # an endpoint child (docs/JOBS.md "Endpoint children"): the run it belongs to, its unit, the attempt that asked
        "child": (
            {
                "root": ann[f"{LABEL}/child-root"],
                "unit": ann.get(f"{LABEL}/child-unit"),
                "parent": ann.get(f"{LABEL}/child-of"),
            }
            if ann.get(f"{LABEL}/child-root")
            else None
        ),
        "gpu_class": labels.get(f"{LABEL}/gpu-class"),
        "image": ann.get(f"{LABEL}/image"),
        "resumable": status in ("FAILED", "CANCELLED", "EXPORT_FAILED")
        and labels.get(f"{LABEL}/mode") == "run"
        and (ann.get(f"{LABEL}/checkpoints") == "shared" if is_jobset(job) else bool(ann.get(f"{LABEL}/pvc"))),
        "execution": _json(ann.get(f"{LABEL}/execution")),
        "reason": (waiting if status == "QUEUED" else None)
        or ann.get(f"{LABEL}/fallback")
        or ann.get(f"{LABEL}/recovery"),
        "phase": phase,
        "successor": ann.get(f"{LABEL}/successor"),
        "recovered_from": ann.get(f"{LABEL}/recovered-from"),
        "nodes": int(ann[f"{LABEL}/nodes"]) if f"{LABEL}/nodes" in ann else 1,
        "interconnect": ann.get(f"{LABEL}/interconnect"),
        "gpu_seconds": measured(ann.get(f"{LABEL}/gpu-seconds")),
        "usage": usage_of(ann),
        "cost": measured(ann[f"{LABEL}/billed"])
        if f"{LABEL}/billed" in ann
        else (price_per_call if status == "SUCCEEDED" and price_per_call is not None else None),
    }


def gpu_seconds(job: dict, records: list | None = None) -> float:
    """Main-container running time x GPUs, summed over every attempt: live pods plus the uploader's
    attempt records for pods that preemption deleted."""
    total = 0.0
    for a in _attempts(job.get("_pods", []), records, job.get("metadata", {}).get("name")):
        d = _secs(a["started_at"], a["ended_at"])
        if d and a["_gpus"]:
            total += d * a["_gpus"]
    return round(total, 1)


def finished(job: dict) -> bool:
    return any(
        c.get("type") in ("Complete", "Failed") and c.get("status") == "True"
        for c in (job.get("status", {}).get("conditions") or [])
    )
