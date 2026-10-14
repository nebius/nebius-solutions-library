"""Per-node fit (docs/SCHEDULING.md "Per-node fit"): Kueue admits a workload against a flavor's AGGREGATE quota
(max_nodes x per-node budget) and never asks whether ONE node holds the pod. A GPU-free run that the CPU pool's quota
refuses for the moment falls through the flavor order to a GPU pool whose aggregate CPU covers it while no single
node does (an AlphaFold 3 pipeline at 16.1 CPU on 1-GPU nodes of 14 admittable CPU), and its pod stays Pending for
ever on an admitted workload that holds the GPU flavor's quota. The fleet chart publishes what one node of every pool
admits (`fit.yaml` of the fleet document, from the same helpers that size the quotas) and the API:

- refuses at submission a pod shape that no node of any candidate pool holds (400 `impossible shape`, naming the
  largest candidate node), the explicit `pool:` of a class included (never silently enlarged or reduced);
- constrains every other run to the pools whose node holds it (`serverless2.nebius/pool In [...]`, conjunctive with the
  class, shared-fs and local-NVMe terms), so Kueue waits on a pool that can run it (QUEUED, "temporarily unavailable")
  instead of admitting it where it can never schedule;
- lets the reconciler tell an admitted-but-unschedulable run whose assigned pool cannot hold it (an impossible fit on
  that flavor) from one that merely waits for a node (reconciliation.unschedulable_switch).

Without `fit.yaml` (an older fleet chart) nothing changes: no constraint, no refusal."""

from __future__ import annotations

import re

import kube

_UNITS = {
    "": 1,
    "m": 1e-3,
    "k": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
    "Ki": 2**10,
    "Mi": 2**20,
    "Gi": 2**30,
    "Ti": 2**40,
}
GIB = float(2**30)


def quantity(value) -> float:
    """A Kubernetes quantity as a float (cores for cpu, bytes for memory/storage, count for devices)."""
    if value is None:
        return 0.0
    s = str(value).strip()
    m = re.fullmatch(r"([0-9]*\.?[0-9]+)(m|k|M|G|T|Ki|Mi|Gi|Ti)?", s)
    if not m:
        raise ValueError(f"not a quantity: {value!r}")
    return float(m.group(1)) * _UNITS[m.group(2) or ""]


def pod_requests(pod: dict) -> dict:
    """The pod's effective requests as the scheduler sees them: the sum over the app containers, each resource no
    lower than any init container's own request (Kubernetes' effective pod request)."""
    keys = {"cpu": "cpu", "memory": "memory", "ephemeral": "ephemeral-storage", "gpu": "nvidia.com/gpu"}
    total = {k: 0.0 for k in keys}
    for c in pod.get("containers") or []:
        req = (c.get("resources") or {}).get("requests") or {}
        for k, rk in keys.items():
            total[k] += quantity(req.get(rk))
    for c in pod.get("initContainers") or []:
        req = (c.get("resources") or {}).get("requests") or {}
        for k, rk in keys.items():
            total[k] = max(total[k], quantity(req.get(rk)))
    return total


def table() -> dict:
    """{cluster id: {pool label: {kind, gpus, cpu, memory_gib, ephemeral_gib, shared_fs}}} or {} without fit.yaml."""
    return kube.fleet().get("fit") or {}


def holds(node: dict, req: dict) -> bool:
    """Whether one node of a pool (a fit.yaml entry) admits the requests."""
    try:
        return (
            req["cpu"] <= float(node.get("cpu") or 0)
            and req["memory"] <= float(node.get("memory_gib") or 0) * GIB
            and req["ephemeral"] <= float(node.get("ephemeral_gib") or 0) * GIB
            and req["gpu"] <= float(node.get("gpus") or 0)
        )
    except (TypeError, ValueError):
        return False


def _capable(cid: str, pool: str, node: dict, shared_fs: bool, local_nvme: bool) -> bool:
    """The pool's node has what the run requires besides size: the shared filesystem (fit.yaml `shared_fs`, the
    flavor's nodeLabels) and local NVMe (pools.yaml `local_nvme` of that cluster's pool); intersected per
    (cluster, pool), never across clusters."""
    if shared_fs and not node.get("shared_fs"):
        return False
    if local_nvme:
        return any(
            p.get("region") == cid and p.get("pool") == pool and p.get("local_nvme") in (True, "true")
            for p in (kube.fleet().get("pools") or {}).values()
        )
    return True


def candidates(
    clusters: list[str] | None = None,
    pools: set[str] | None = None,
    shared_fs: bool = False,
    local_nvme: bool = False,
    gpu_free: bool = False,
):
    """(cluster id, pool label, node) of every candidate pool: on `clusters` (or everywhere), among `pools` (or all),
    with the required capabilities; `gpu_free`: only pools whose nodes carry no GPU (the system and CPU pools: a pod
    that requests no GPU and must not occupy a GPU node, e.g. an endpoint caller)."""
    for cid, by_pool in table().items():
        if clusters is not None and cid not in clusters:
            continue
        for pool, node in (by_pool or {}).items():
            if pools is not None and pool not in pools:
                continue
            if gpu_free and (node or {}).get("kind") == "gpu":
                continue
            if _capable(cid, pool, node or {}, shared_fs, local_nvme):
                yield cid, pool, node or {}


def fitting(
    req: dict,
    clusters: list[str] | None = None,
    pools: set[str] | None = None,
    shared_fs: bool = False,
    local_nvme: bool = False,
    gpu_free: bool = False,
) -> dict[str, list[str]]:
    """{pool label: [cluster ids]} of the candidate pools (see `candidates`) whose node holds the requests."""
    out: dict[str, list[str]] = {}
    for cid, pool, node in candidates(clusters, pools, shared_fs, local_nvme, gpu_free):
        if holds(node, req):
            out.setdefault(pool, []).append(cid)
    return {k: sorted(v) for k, v in sorted(out.items())}


def describe(req: dict) -> str:
    gpu = f", {req['gpu']:g} GPU" if req["gpu"] else ""
    return (
        f"cpu {req['cpu']:g}, memory {req['memory'] / GIB:.2f}Gi, ephemeral-storage {req['ephemeral'] / GIB:.2f}Gi{gpu}"
    )


def largest(
    clusters: list[str] | None = None, pools: set[str] | None = None, shared_fs: bool = False, local_nvme: bool = False
) -> str:
    """The roomiest candidate node (same candidates as `fitting`), for the refusal message: 'cpu 28, memory 112Gi,
    ephemeral-storage 50Gi (pool cpu-batch-32 on north)'."""
    best, where = None, ""
    for cid, pool, node in candidates(clusters, pools, shared_fs, local_nvme):
        key = (float(node.get("cpu") or 0), float(node.get("memory_gib") or 0), float(node.get("ephemeral_gib") or 0))
        if best is None or key > best:
            best, where = key, f"pool {pool} on {cid}"
    if best is None:
        return "no candidate pool"
    return f"cpu {best[0]:g}, memory {best[1]:g}Gi, ephemeral-storage {best[2]:g}Gi ({where})"


def assigned_pool_holds(pod: dict, cluster: str | None) -> bool | None:
    """For an admitted pod: whether the pool Kueue assigned it (nodeSelector serverless2.nebius/pool, the flavor's
    label) ON THE CLUSTER THAT ADMITTED IT has a node that holds its requests; None when the cluster, the table or
    that cluster's pool is unknown (no judgement). The same pool label can name a bigger preset in another cluster:
    only the observed worker's entry counts."""
    from config import LABEL

    spec = pod.get("spec") or {}
    pool = (spec.get("nodeSelector") or {}).get(f"{LABEL}/pool")
    if not pool or not cluster or not table():
        return None
    node = (table().get(cluster) or {}).get(pool)
    if not node:
        return None
    return holds(node, pod_requests(spec))
