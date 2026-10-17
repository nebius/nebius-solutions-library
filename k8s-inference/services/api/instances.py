"""instances: where an attempt or an endpoint replica ran, for the operator: the pool, the instance type, the node group and
the Nebius console page of the node (docs/API.md "Placement"), and who asked for it (`requester`, an opaque block
the submitting application sends with :invoke; docs/API.md "Requester").

Serverless 2 publishes this to its own API and console. A domain platform built on top of it (a scientific
workbench, say) is the abstraction its customers see and forwards none of it.
"""

from __future__ import annotations

import json
import re

from fastapi import HTTPException

import kube
from config import LABEL

REQUESTER_ANNOTATION = f"{LABEL}/requester"
REQUESTER_MAX_KEYS = 8
REQUESTER_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
REQUESTER_VALUE_MAX = 128


def validate_requester(value: dict | None) -> dict | None:
    """The `requester` block of :invoke: at most eight `snake_case` keys, string values of at most 128 characters
    (a team, a user, a customer id; never a secret: the block is stored on the Job and returned on every read).
    None stays None; {} is refused (send nothing instead)."""
    if value is None:
        return None
    if not isinstance(value, dict) or not value:
        raise HTTPException(400, "requester: a non-empty object of string values")
    if len(value) > REQUESTER_MAX_KEYS:
        raise HTTPException(400, f"requester: at most {REQUESTER_MAX_KEYS} keys")
    out = {}
    for k, v in value.items():
        if not isinstance(k, str) or not REQUESTER_KEY.match(k):
            raise HTTPException(400, "requester: keys are snake_case, 1..32 characters")
        if not isinstance(v, str) or not v.strip() or len(v) > REQUESTER_VALUE_MAX:
            raise HTTPException(400, f"requester.{k}: a string of 1..{REQUESTER_VALUE_MAX} characters")
        out[k] = v
    return out


def annotate_requester(job: dict, requester: dict | None) -> None:
    if requester:
        job["metadata"].setdefault("annotations", {})[REQUESTER_ANNOTATION] = json.dumps(requester, sort_keys=True)


def requester_of(ann: dict | None) -> dict | None:
    """The block as the submission stored it (a continuation carries the original's: jobs._inherit_owner)."""
    raw = (ann or {}).get(REQUESTER_ANNOTATION)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) and value else None


def inherit_requester(job: dict, ann: dict) -> None:
    raw = (ann or {}).get(REQUESTER_ANNOTATION)
    if raw:
        job["metadata"].setdefault("annotations", {})[REQUESTER_ANNOTATION] = raw


def fields(cluster: str | None, node: str | None, pool: str | None) -> dict:
    """{pool, instance_type, node_group, console_url} for a node of a cluster: the node's labels while it exists
    (the exact instance), else what the fleet document knows about the pool (its platform/preset and node group: the
    kind of instance, not the one; `instance_exact: false` then). `instance_type` is `<platform>/<preset>`."""
    region = kube.cluster_region(cluster) if cluster else None
    live = kube.node_placement(region, node) if region else None
    pool = pool or (live or {}).get("pool")
    entry = kube.pool_entry(cluster, pool)
    out: dict = {"pool": pool}
    if live and live.get("instance_type"):
        out["instance_type"] = f"{live['instance_type']}/{live.get('preset') or '?'}"
        out["node_group"] = live.get("node_group")
        out["instance_exact"] = True
    elif entry.get("platform"):
        out["instance_type"] = f"{entry['platform']}/{entry.get('preset') or '?'}"
        out["node_group"] = entry.get("node_group_id")
        out["instance_exact"] = False
    else:
        return out
    out["console_url"] = kube.console_node_url(cluster, out.get("node_group"), node)
    return out


def enrich_attempts(attempts: list | None, cluster: str | None) -> list | None:
    """Adds the placement fields to every attempt that names a node (or a pool), in place (and returns the same list).
    `cluster` is the operation's cluster id; an attempt may carry its own (a chain member on another cluster)."""
    for a in attempts or []:
        if isinstance(a, dict) and (a.get("node") or a.get("pool")):
            a.update(fields(a.get("cluster") or cluster, a.get("node"), a.get("pool")))
    return attempts


placed = enrich_attempts  # as an expression


def enrich_operation(op: dict) -> dict:
    enrich_attempts(op.get("attempts"), op.get("cluster"))
    return op


def replicas(pods: list, cluster: str | None) -> list[dict]:
    """The endpoint's pods as replicas: {pod, ready, phase, node, started_at} plus the placement fields of the
    node (docs/API.md "Placement"). `pods` are kubernetes client V1Pod objects."""
    out = []
    for p in pods:
        # attribute access with defaults: a fake or a partial object (tests, a list from a cache) has no spec
        st, spec = getattr(p, "status", None), getattr(p, "spec", None)
        phase, node, start = (
            getattr(st, "phase", None),
            getattr(spec, "node_name", None),
            getattr(st, "start_time", None),
        )
        entry = {
            "pod": getattr(getattr(p, "metadata", None), "name", None),
            "ready": bool(phase == "Running" and all(c.ready for c in (getattr(st, "container_statuses", None) or []))),
            "phase": phase,
            "node": node,
            "started_at": start.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(start, "strftime") else None,
        }
        if node:
            entry.update(fields(cluster, node, (getattr(spec, "node_selector", None) or {}).get(f"{LABEL}/pool")))
        out.append(entry)
    return sorted(out, key=lambda e: e["pod"] or "")
