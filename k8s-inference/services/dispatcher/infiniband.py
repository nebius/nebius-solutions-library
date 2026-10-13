"""InfiniBand pools that grow on demand (docs/SCHEDULING.md "InfiniBand pools grow on demand").

The Nebius cluster autoscaler never adds an InfiniBand node: it simulates the new node without the DRA attributes
of its RDMA NICs, so a pod that claims them never triggers a scale-up (docs/JOBS.md "Multi-node runs").
Without this an operator would raise the pool's `min_nodes` by hand before a multi-node run.
The dispatcher's elected replica does it instead, every reconcile cycle, per worker cluster:
  1. demand per InfiniBand pool = the largest node count any one multi-node run or endpoint there needs now:
     Kueue Workloads (the JobSets of multi-node runs) whose pod template requires the node label
     `serverless2.nebius/interconnect=infiniband`, pending or admitted and not finished (nodes = the pod set's
     count; the pool = Kueue's flavor assignment once quota is reserved, before that the InfiniBand pool the
     template's `serverless2.nebius/pool` affinity allows that Kueue will take first: reserved, on-demand, spot,
     then price), plus LeaderWorkerSets of multi-node endpoints (services/api/render_endpoint.py) with replicas > 0 pinned to an
     InfiniBand pool (nodes = size x replicas);
  2. the pool's floor = max(the fleet document's `min_nodes`, demand), never above `max_nodes` (a run that needs
     more nodes than the pool has can never fit there: logged once, nothing changed). Raised at once with
     `nebius mk8s node-group update --patch --autoscaling-min-node-count N` as the region's ops identity
     (nebius_cli.py); lowered only once demand has stayed below the floor for RESTORE_AFTER_S, so a restart-all
     after a preemption finds its nodes still there and a run that follows another reuses them;
  3. what the dispatcher set is kept in the ConfigMap kueue-system/infiniband-pools (`state.json`, created on
     first use; runtime-owned, outside Terraform): a restarted dispatcher still restores the fleet's value when
     the pool goes idle, and `terraform plan` of the cloud stage is clean again.
Pools without `interconnect: infiniband` or without a node group id (the cloud stage has not created it yet) are
never touched; a failed CLI call is logged and retried on the next cycle (the patch is idempotent).
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

from kubernetes import client

import nebius_cli

log = logging.getLogger("dispatcher.infiniband")
GROUP, VERSION = "kueue.x-k8s.io", "v1beta2"
LWS = ("leaderworkerset.x-k8s.io", "v1", "leaderworkersets")
LABEL = "serverless2.nebius"
POOL_KEY, IB_KEY = f"{LABEL}/pool", f"{LABEL}/interconnect"
NS = os.environ.get("PRICES_NAMESPACE", "kueue-system")
STATE_CM = os.environ.get("INFINIBAND_CONFIGMAP", "infiniband-pools")
RESTORE_AFTER_S = int(os.environ.get("RESTORE_AFTER_S", "600"))
CAP_RANK = {
    "reserved": 0,
    "on_demand": 1,
    "ondemand": 1,
    "spot": 2,
}  # fleet.yaml capacity_order, as Kueue's flavor order
_warned: set = set()  # (cluster, workload) already reported as too large for its pool


# ---------- pure functions (unit-tested) ----------
def ib_pools(cluster: dict) -> dict:
    """{pool: pool} of a cluster's pools (dispatcher.clusters_from_pools) whose nodes form an InfiniBand GPU cluster
    and whose node group the fleet document names (charts/fleet pools.yaml `node_group_id`)."""
    return {
        n: p
        for n, p in (cluster.get("pools") or {}).items()
        if (p.get("interconnect") or "none") == "infiniband" and p.get("node_group_id")
    }


def _required(template: dict) -> dict:
    """Node label -> values a pod template requires (required nodeAffinity `In` terms plus nodeSelector)."""
    spec = template.get("spec") or {}
    out: dict = {}
    for t in (
        ((spec.get("affinity") or {}).get("nodeAffinity") or {}).get("requiredDuringSchedulingIgnoredDuringExecution")
        or {}
    ).get("nodeSelectorTerms") or []:
        for e in t.get("matchExpressions") or []:
            if e.get("operator") == "In":
                out.setdefault(e.get("key"), set()).update(e.get("values") or [])
    for k, v in (spec.get("nodeSelector") or {}).items():
        out.setdefault(k, set()).add(v)
    return out


def _first(candidates: set, pools: dict) -> str:
    """The pool Kueue takes first among candidates: reserved, on-demand, spot (capacity_order), then price, then name."""
    return min(
        candidates,
        key=lambda n: (CAP_RANK.get(pools[n].get("capacity", "spot"), 3), float(pools[n].get("price", 1e9)), n),
    )


def finished(wl: dict) -> bool:
    if (wl.get("spec") or {}).get("active") is False:  # deactivated (a superseded run): never admitted again
        return True
    return any(
        c.get("type") == "Finished" and c.get("status") == "True"
        for c in (wl.get("status") or {}).get("conditions") or []
    )


def workload_demand(wl: dict, pools: dict) -> dict:
    """{pool: nodes} an unfinished Workload needs on the InfiniBand pools `pools` (ib_pools): a pod set that Kueue
    assigned to one of them, or one whose template requires the InfiniBand label (the pool it will get is the
    first of its affinity's candidates); {} for anything else (single-node runs, Ethernet multi-node runs)."""
    if finished(wl):
        return {}
    assigned = {
        a.get("name"): (a.get("flavors") or {}).get("nvidia.com/gpu")
        for a in ((wl.get("status") or {}).get("admission") or {}).get("podSetAssignments") or []
    }
    out: dict = {}
    for ps in (wl.get("spec") or {}).get("podSets") or []:
        req = _required(ps.get("template") or {})
        pool = assigned.get(ps.get("name"))
        if pool not in pools:
            if "infiniband" not in req.get(IB_KEY, set()):
                continue
            cands = (req.get(POOL_KEY) or set(pools)) & set(pools)
            if not cands:
                continue
            pool = _first(cands, pools)
        out[pool] = out.get(pool, 0) + int(ps.get("count") or 1)
    return out


def lws_demand(obj: dict, pools: dict) -> dict:
    """{pool: nodes} a started multi-node endpoint (LeaderWorkerSet, services/api/render_endpoint.py) holds on an InfiniBand pool:
    its pods pin the pool with a nodeSelector; size x replicas whole nodes while replicas > 0."""
    spec = obj.get("spec") or {}
    replicas = int(spec.get("replicas") or 0)
    lwt = spec.get("leaderWorkerTemplate") or {}
    size = int(lwt.get("size") or ((obj.get("metadata") or {}).get("annotations") or {}).get(f"{LABEL}/nodes") or 1)
    if replicas < 1:
        return {}
    cands = (_required(lwt.get("leaderTemplate") or lwt.get("workerTemplate") or {}).get(POOL_KEY) or set()) & set(
        pools
    )
    return {_first(cands, pools): size * replicas} if cands else {}


def demand(workloads: list, lwss: list, pools: dict, cluster: str = "") -> dict:
    """{pool: nodes} = the largest single demand per pool (every run holds whole nodes of its own; the floor serves
    the biggest one, the autoscaler's own scale-up covers nothing here). A demand above the pool's `max_nodes`
    is dropped (it can never fit) and reported once."""
    out: dict = {}
    for kind, items, fn in (("workload", workloads, workload_demand), ("endpoint", lwss, lws_demand)):
        for obj in items:
            md = obj.get("metadata") or {}
            for pool, n in fn(obj, pools).items():
                if n > int(pools[pool].get("max_nodes") or 0):
                    key = (cluster, md.get("namespace"), md.get("name"))
                    if key not in _warned:
                        _warned.add(key)
                        log.warning(
                            "%s: %s %s/%s needs %d nodes on %s, more than its max_nodes %s: it cannot run there",
                            cluster,
                            kind,
                            md.get("namespace"),
                            md.get("name"),
                            n,
                            pool,
                            pools[pool].get("max_nodes"),
                        )
                    continue
                out[pool] = max(out.get(pool, 0), n)
    return out


def plan(pools: dict, needed: dict, state: dict, now: float) -> list:
    """Floors to set: [(pool, target, reason)]. `state` is this cluster's {pool: {"set": floor the dispatcher set,
    "low_since": epoch}}; `set` absent = the fleet's own `min_nodes`. A raise is immediate; a lower waits until
    demand has been below the floor for RESTORE_AFTER_S (`low_since`, kept here so a failed call retries at once)."""
    changes = []
    for name, p in pools.items():
        base = int(p.get("min_nodes") or 0)
        need = int(needed.get(name, 0))
        s = state.setdefault(name, {})
        cur = int(s["set"]) if s.get("set") is not None else base
        target = max(base, need)
        if target > cur:
            changes.append((name, target, f"demand {need} nodes"))
        elif target < cur:
            since = s.get("low_since")
            if since is None:
                s["low_since"] = now
            elif now - float(since) >= RESTORE_AFTER_S:
                changes.append(
                    (
                        name,
                        target,
                        "idle, back to the fleet's min_nodes" if target == base else f"demand down to {need} nodes",
                    )
                )
        else:
            s.pop("low_since", None)
    return changes


# ---------- cluster access ----------
def load_state(core: client.CoreV1Api) -> dict:
    try:
        return json.loads((core.read_namespaced_config_map(STATE_CM, NS).data or {}).get("state.json") or "{}")
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise
        return {}


def save_state(core: client.CoreV1Api, state: dict) -> None:
    body = {
        "metadata": {
            "name": STATE_CM,
            "labels": {"app.kubernetes.io/part-of": "serverless2", "app.kubernetes.io/name": "dispatcher"},
        },
        "data": {"state.json": json.dumps(state, sort_keys=True)},
    }
    try:
        core.patch_namespaced_config_map(STATE_CM, NS, body)
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise
        core.create_namespaced_config_map(NS, body)


def list_lws(w: dict) -> list:
    """Every LeaderWorkerSet of a worker; none when the CRD is not installed there."""
    try:
        return w["custom"].list_cluster_custom_object(*LWS).get("items", [])
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise
        return []


def set_min_nodes(node_group_id: str, n: int, region: str) -> None:
    nebius_cli.run(
        [
            "mk8s",
            "node-group",
            "update",
            "--id",
            node_group_id,
            "--patch",
            "--autoscaling-min-node-count",
            str(n),
            "--format",
            "json",
        ],
        region=region,
        timeout=180,
    )


def reconcile(core: client.CoreV1Api, workers: dict, clusters: dict, aliases: dict) -> None:
    """One pass over every worker. `workers`: dispatcher.worker_clients(); `clusters`: dispatcher.clusters_from_pools()
    (pools with interconnect, node_group_id, min_nodes, max_nodes); `aliases`: {region name: cluster id} (the key
    file, and so the CLI profile, is per region name)."""
    names = {cid: r for r, cid in aliases.items()}
    state = load_state(core)
    before = json.dumps(state, sort_keys=True)
    now = time.time()
    seen: dict = {}
    for cid, w in workers.items():
        pools = ib_pools(clusters.get(cid) or {})
        if not pools:
            continue
        try:
            workloads = w["custom"].list_cluster_custom_object(GROUP, VERSION, "workloads").get("items", [])
            lwss = list_lws(w)
        except Exception as e:  # noqa: BLE001 - an unreachable worker keeps its floors until the next cycle
            log.warning("worker %s: workloads: %s", cid, e)
            seen[cid] = state.get(cid) or {}
            continue
        needed = demand(workloads, lwss, pools, cid)
        cstate = {n: s for n, s in (state.get(cid) or {}).items() if n in pools}  # pools gone from the fleet: forgotten
        for pool, target, reason in plan(pools, needed, cstate, now):
            p, region = pools[pool], names.get(cid)
            if not region or not nebius_cli.profile(region):
                log.warning(
                    "%s/%s: min_nodes -> %d: no credentials for region %s mounted (/etc/nebius/<region>.json)",
                    cid,
                    pool,
                    target,
                    region,
                )
                continue
            try:
                set_min_nodes(p["node_group_id"], target, region)
            except Exception as e:  # noqa: BLE001 - logged, retried next cycle
                log.warning(
                    "%s/%s: min_nodes -> %d failed: %s",
                    cid,
                    pool,
                    target,
                    (getattr(e, "stderr", None) or str(e)).strip(),
                )
                continue
            log.info(
                "%s/%s: min_nodes %s -> %d (%s; fleet min %s, max %s, node group %s)",
                cid,
                pool,
                cstate[pool].get("set", p.get("min_nodes")),
                target,
                reason,
                p.get("min_nodes"),
                p.get("max_nodes"),
                p["node_group_id"],
            )
            # at the fleet's own value nothing is remembered: the state is empty while every pool is idle
            cstate[pool] = (
                {"set": target, "at": datetime.now(timezone.utc).isoformat()}
                if target != int(p.get("min_nodes") or 0)
                else {}
            )
        seen[cid] = {n: s for n, s in cstate.items() if s.get("set") is not None or s.get("low_since") is not None}
    state = {cid: s for cid, s in seen.items() if s}
    if json.dumps(state, sort_keys=True) != before:
        save_state(core, state)
