"""Which GPU class a run gets when its class decides the image (docs/SCHEDULING.md "Per-GPU images for runs").

A run class may name one image per GPU class (`job.images`). Kueue picks the pool at admission and a Job's image
cannot change afterwards, so for such a class the choice happens here, at submission, from the fleet document
(`kube.fleet()`, the fleet-prices ConfigMap) in the order the dispatcher ranks pools (services/dispatcher
`rank()`): the model's class preference, then reserved before on-demand before spot, then what the run would cost
on the pool at its list price. The Job is rendered with the chosen class's image and queued on that class's
profile. It may still move between regions of the class while it waits (MultiKueue), and a resume keeps the class.
Runs whose class has one image are not affected: they keep switching class at queue time.

What this does NOT know, by design: whether a pool has free quota right now and the live spot quote. Without that
signal every pool is costed as if it had to boot a node, which is the dispatcher's own rule for a pool that is not
free (a 1-GPU run does not pick an 8-GPU pool over a 1-GPU one), and the queue waits for the preferred class either
way."""

import kube
from render import class_images, gpu_classes

CAP_RANK = {"reserved": 0, "on_demand": 1, "ondemand": 1, "spot": 2}  # fleet.yaml capacity_order, as in the dispatcher


def rank_pools(
    pools: dict,
    classes: list[str],
    gpus: int,
    regions: list[str] | None = None,
    pin: str | None = None,
    tenant: str | None = None,
) -> list[dict]:
    """The fleet's pools a run may use, best first (the dispatcher's `rank()` order without its free-quota signal).

    pools:   the fleet document's pools ({name: {region (cluster id), pool, gpu_class, capacity, usd_per_gpu_hour,
             gpus_per_node, tenant}})
    classes: the GPU classes the run may use, preferred first (the model's `gpu.classes`, narrowed by the caller)
    regions: region names the model is deployed in (None = anywhere); pin: one region (name or cluster id)
    tenant:  the run's tenant; a pool dedicated to another tenant (fleet tenants.<name>.dedicated_pools) is never a candidate
    """
    want = max(int(gpus), 1)
    cands = []
    for p in pools.values():
        cluster = p.get("region") or ""
        region = kube.cluster_region(cluster)
        if p.get("gpu_class") not in classes or (p.get("tenant") and p.get("tenant") != tenant):
            continue
        if regions is not None and region not in regions and cluster not in regions:
            continue
        if pin and pin not in (region, cluster):
            continue
        node = int(p.get("gpus_per_node") or 1)
        cap = p.get("capacity") or "spot"
        price = 0.0 if cap == "reserved" else float(p.get("usd_per_gpu_hour") or 1e9)  # reserved: marginal cost 0
        cands.append(
            {
                "cluster": cluster,
                "region": region,
                "pool": p.get("pool"),
                "gpu_class": p["gpu_class"],
                "capacity": cap,
                "price": price,
                "cost": price * max(want, node),
            }
        )  # the node the pool would boot, not just the run's GPUs
    return sorted(
        cands,
        key=lambda c: (
            classes.index(c["gpu_class"]),
            CAP_RANK.get(c["capacity"], 3),
            c["cost"],
            c["cluster"],
            c["pool"],
        ),
    )


def choose_class(
    model: dict,
    region: str | None,
    gpus: int,
    available: list[str] | None = None,
    tenant: str | None = None,
    regions: list[str] | None = None,
) -> tuple[str, str]:
    """(gpu class, how it was chosen): the class of the best pool in the fleet document among the model's classes
    (`available`: the caller's narrowing and order when given, None = the model's own) and regions (`regions`: the
    caller's allowed clusters when given, None = the model's own), pinned to `region` when given. Without any such
    pool the first allowed class is taken; the queue then waits for it."""
    classes = gpu_classes(model)
    images = class_images(model) or {}
    allowed = [c for c in classes if c in images or "default" in images] or classes
    if available is not None:
        allowed = [c for c in available if c in allowed] or allowed
    ranked = rank_pools(
        kube.fleet().get("pools") or {}, allowed, gpus, regions or model.get("regions") or None, region, tenant
    )
    if ranked:
        best = ranked[0]
        return best["gpu_class"], f"fleet ({best['region']}/{best['pool']}, {best['capacity']}, ${best['price']}/GPU-h)"
    return allowed[0], "preferred (no pool)"
