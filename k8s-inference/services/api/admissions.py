"""Admission records of runs (docs/JOBS.md "Resume", "restart from inputs"): the Job's labels and annotations (input,
class binding, execution block, owner, key, lineage), its region, timeout and output prefix, written by `jobs.create` and
kept past the Job's TTL, so `:resume` of a FAILED root whose Job and work volume expired can render the run again from
its inputs on a fresh volume, as a continuation of the same root. Only runs admitted from api 0.11.74 on have a
record; an older root's resume is refused as before (never a reconstruction from thin air)."""

from __future__ import annotations

import json
import logging

import db
from config import LABEL

log = logging.getLogger("api.admissions")


def enabled() -> bool:
    return db.enabled()


def record(job: dict, region: str | None, output_prefix: str | None) -> None:
    """Idempotent (a replayed create writes the same row). Never raises into the submission: a missed record only
    means a later resume after the TTL is refused, as before 0.11.74."""
    if not enabled():
        return
    md = job.get("metadata") or {}
    labels, ann = dict(md.get("labels") or {}), dict(md.get("annotations") or {})
    if labels.get(f"{LABEL}/mode") != "run":
        return
    spec = job.get("spec") or {}
    timeout = spec.get("activeDeadlineSeconds")
    try:
        with db.pool().connection() as c:
            c.execute(
                """insert into run_admissions (operation, tenant, region, labels, annotations, timeout_s, output_prefix, managed_by)
                   values (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s) on conflict (operation) do nothing""",
                (
                    md["name"],
                    labels.get(f"{LABEL}/tenant") or "",
                    region,  # a fleet-placed run: the manager's cluster (the worker is chosen at placement, not here)
                    json.dumps(labels),
                    json.dumps(ann),
                    int(timeout) if timeout not in (None, 1) else None,
                    output_prefix,
                    spec.get("managedBy"),  # set for a fleet-placed run: its restart is placed by the fleet again
                ),
            )
    except Exception as e:  # noqa: BLE001
        log.warning("admission record of %s not written: %s", md.get("name"), e)


def get(operation: str) -> dict | None:
    if not enabled():
        return None
    with db.pool().connection() as c:
        r = c.execute(
            "select operation, tenant, region, labels, annotations, timeout_s, output_prefix, managed_by "
            "from run_admissions where operation = %s",
            (operation,),
        ).fetchone()
    if r is None:
        return None
    return {
        "operation": r[0],
        "tenant": r[1],
        "region": r[2],
        "labels": r[3] or {},
        "annotations": r[4] or {},
        "timeout_s": r[5],
        "output_prefix": r[6],
        "managed_by": r[7],
    }


def as_job(rec: dict, status: str | None = None) -> dict:
    """The expired Job as `jobs.resume` needs it: its metadata, the deadline, the uploader's output prefix, the
    MultiKueue `managedBy` of a fleet-placed run (its restart is placed by the fleet again, never pinned to the
    manager's cluster), and the terminal state the durable attempt rows report (`status`: SUCCEEDED | CANCELLED |
    FAILED; FAILED when nothing is on record: it never started)."""
    done = {"SUCCEEDED": "Complete"}.get(status or "", "Failed")
    managed = rec.get("managed_by")
    return {
        "metadata": {"name": rec["operation"], "labels": dict(rec["labels"]), "annotations": dict(rec["annotations"])},
        "spec": {
            **({"activeDeadlineSeconds": rec["timeout_s"]} if rec.get("timeout_s") else {}),
            **({"managedBy": managed} if managed else {}),
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "uploader",
                            "env": [{"name": "OUTPUT_PREFIX", "value": rec.get("output_prefix") or ""}],
                        }
                    ]
                }
            },
        },
        "status": {"conditions": [{"type": done, "status": "True"}]},
        # a fleet-placed run's worker is chosen again at placement; a single-cluster run restarts on its own cluster
        "_region": None if managed else rec.get("region"),
        "_expired": True,
        "_status": status,
    }
