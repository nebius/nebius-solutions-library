"""From a Job (or JobSet) as read from Kubernetes to the operation the API returns (docs/JOBS.md "Status mapping").

`normalise` is the one shape of an operation; `_attempts` lists one attempt per pod, with the uploader's
attempt records from the bucket standing in for pods that preemption deleted; `gpu_seconds` is what the
billing CronJob prices. The predicates (`is_jobset`, `is_manager_job`, `is_mirror`) tell the kinds apart."""
import json, urllib.parse
from datetime import datetime, timezone
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


def _main_state(pod: dict) -> dict:
    for c in (pod.get("status", {}).get("containerStatuses") or []):
        if c.get("name") == "main":
            return c.get("state") or {}
    return {}


def _gpus(pod: dict) -> int:
    for c in pod.get("spec", {}).get("containers", []):
        if c.get("name") == "main":
            return int(((c.get("resources") or {}).get("limits") or {}).get("nvidia.com/gpu", 0) or 0)
    return 0


RECORD_STATUS = {"succeeded": "SUCCEEDED", "failed": "FAILED", "interrupted": "PREEMPTED"}


def _attempts(pods: list, records: list | None = None, name: str | None = None) -> list[dict]:
    """One attempt per pod, live pods first; attempt records from the bucket (uploader-written,
    docs/JOBS.md) stand in for pods that preemption or node loss deleted. `_gpus` is internal."""
    out, seen = [], set()
    for p in pods:
        st, phase = p.get("status", {}), p.get("status", {}).get("phase", "Pending")
        state = _main_state(p)
        term, run = state.get("terminated") or {}, state.get("running") or {}
        disrupted = any(c.get("type") == "DisruptionTarget" and c.get("status") == "True" for c in (st.get("conditions") or []))
        if term:                                  # the attempt is `main`: the uploader may still be running after it
            s = "SUCCEEDED" if term.get("exitCode") == 0 else ("PREEMPTED" if disrupted or term.get("exitCode") in (137, 143) else "FAILED")
        elif run:
            s = "PREEMPTED" if p["metadata"].get("deletionTimestamp") else "RUNNING"
        else:
            s = "PREEMPTED" if (phase == "Failed" and disrupted) or p["metadata"].get("deletionTimestamp") else PHASE.get(phase, "QUEUED")
        reason = term.get("reason") if term else (st.get("reason") or None)
        if disrupted:
            reason = next((c.get("message") for c in st["conditions"] if c.get("type") == "DisruptionTarget"), reason)
        seen.add(p["metadata"]["name"])
        out.append({"started_at": term.get("startedAt") or run.get("startedAt") or st.get("startTime"),
                    "ended_at": term.get("finishedAt"), "status": s, "node": p["spec"].get("nodeName"),
                    "gpu_class": (p["metadata"].get("labels") or {}).get(f"{LABEL}/gpu-class") or None,
                    "reason": reason if s != "SUCCEEDED" else None, "exit_code": term.get("exitCode"), "_gpus": _gpus(p)})
    for r in records or []:
        if r.get("pod") in seen or (name and r.get("operation") not in (None, name)):
            continue
        out.append({"started_at": r.get("started_at") or None, "ended_at": r.get("ended_at") or None,
                    "status": RECORD_STATUS.get(r.get("status"), "FAILED"), "node": r.get("node") or None,
                    "gpu_class": r.get("gpu_class") or None,
                    "reason": "pod deleted (preempted)" if r.get("status") == "interrupted" else None,
                    "exit_code": r.get("exit_code"), "_gpus": int(r.get("gpus") or 0)})
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


def normalise(job: dict, price_per_call: float | None = None, records: list | None = None) -> dict:
    md, st, sp = job.get("metadata", {}), job.get("status", {}) or {}, job.get("spec", {})
    labels, ann = md.get("labels", {}), md.get("annotations", {})
    conds = {c.get("type"): c for c in (st.get("conditions") or []) if c.get("status") == "True"}
    attempts = [{k: v for k, v in a.items() if k != "_gpus"} for a in _attempts(job.get("_pods", []), records, md.get("name"))]
    if "Complete" in conds or "SuccessCriteriaMet" in conds:
        status = "SUCCEEDED"
    elif "Failed" in conds or "FailureTarget" in conds:
        status = "CANCELLED" if ann.get(f"{LABEL}/cancelled") else "FAILED"
    elif ann.get(f"{LABEL}/cancelled"):
        status = "CANCELLED"
    elif any(a["status"] == "RUNNING" for a in attempts):
        status = "RUNNING"
    else:
        status = "QUEUED"          # gated by Kueue, pending for a node, being placed, or between attempts after a preemption
    if ann.get(f"{LABEL}/cancelled") and attempts and attempts[-1]["status"] in ("PREEMPTED", "RUNNING", "QUEUED"):
        attempts[-1]["status"], attempts[-1]["reason"] = "CANCELLED", "cancelled"      # the attempt the cancel interrupted
    c = conds.get("Failed") or conds.get("FailureTarget") or {}
    error = None
    if status == "FAILED":
        last = next((a for a in reversed(attempts) if a["status"] == "FAILED"), None)
        error = (last or {}).get("reason") or c.get("message") or c.get("reason")
    try:
        inp = json.loads(ann.get(f"{LABEL}/input", "{}"))
    except ValueError:
        inp = {}
    region = labels.get(f"{LABEL}/region") or job.get("_region") or (None if is_manager_job(job) else REGION)
    if is_jobset(job):              # a JobSet reports no start/completion time of its own: the pods' are the run's
        st = dict(st)
        st["startTime"] = min((a["started_at"] for a in attempts if a["started_at"]), default=None)
        if status in ("SUCCEEDED", "FAILED", "CANCELLED"):
            st["completionTime"] = max((a["ended_at"] or "" for a in attempts), default=None) or None
    ended = st.get("completionTime") or (max((a["ended_at"] or "" for a in attempts), default=None) if status in ("FAILED", "CANCELLED") else None)
    return {"id": md["name"], "name": ann.get(f"{LABEL}/name"), "model": labels.get(f"{LABEL}/model"),
            "mode": labels.get(f"{LABEL}/mode"), "status": status, "region": region, "tenant": labels.get(f"{LABEL}/tenant"),
            "profile": labels.get(f"{LABEL}/profile", KUEUE_QUEUE),
            "priority": labels.get(f"{LABEL}/priority", "normal"), "created_at": md.get("creationTimestamp"),
            "started_at": st.get("startTime"), "ended_at": ended or None,
            "duration_s": _secs(st.get("startTime"), ended), "timeout_s": sp.get("activeDeadlineSeconds") if sp.get("activeDeadlineSeconds") != 1 else None,
            "attempts": attempts, "logs_url": logs_url(md.get("namespace", ""), md["name"], region),
            "error": error, "input": inp, "resumed_from": ann.get(f"{LABEL}/resumed-from"),
            "gpu_class": labels.get(f"{LABEL}/gpu-class"), "image": ann.get(f"{LABEL}/image"),
            "resumable": status in ("FAILED", "CANCELLED") and labels.get(f"{LABEL}/mode") == "run" and
            (ann.get(f"{LABEL}/checkpoints") == "shared" if is_jobset(job) else bool(ann.get(f"{LABEL}/pvc"))),
            "nodes": int(ann[f"{LABEL}/nodes"]) if f"{LABEL}/nodes" in ann else 1,
            "gpu_seconds": float(ann[f"{LABEL}/gpu-seconds"]) if f"{LABEL}/gpu-seconds" in ann else None,
            "cost": float(ann[f"{LABEL}/billed"]) if f"{LABEL}/billed" in ann else (price_per_call if status == "SUCCEEDED" and price_per_call is not None else None)}


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
    return any(c.get("type") in ("Complete", "Failed") and c.get("status") == "True" for c in (job.get("status", {}).get("conditions") or []))
