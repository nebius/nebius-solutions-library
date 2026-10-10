"""Which GPU class a run gets when its class decides the image (docs/SCHEDULING.md "Per-GPU images for runs").

A run class may name one image per GPU class (`job.images`). Kueue picks the pool at admission and a Job's image
cannot change afterwards, so for such a class the choice happens here, at submission: the dispatcher's ranking
(`GET /v1/rank`: class preference, reserved before on-demand before spot, price, free capacity now) names the best
(class, pool, region); the Job is rendered with that class's image and queued on that class's profile. It may still
move between regions of the class while it waits (MultiKueue), and a resume keeps the class. Runs whose class has
one image are not affected: they keep switching class at queue time.

The option "re-render a waiting run for the next class after N minutes" (A+ in docs/DESIGN-REVIEW-2026-10-09.md)
is documented, not built."""
import logging
import httpx
from config import DISPATCHER_URL, RANK_TIMEOUT_S
from render import class_images, gpu_classes

log = logging.getLogger("placement")


def choose_class(model: dict, region: str | None, gpus: int, available: list[str] | None = None) -> tuple[str, str]:
    """(gpu class, how it was chosen): the class of the dispatcher's best candidate among the model's classes (and
    the pinned region, when given); without a usable ranking the first class of the model's preference list that
    has a pool (`available`: pool classes the caller knows of; None = no filter). The fallback keeps submissions
    working when the dispatcher is restarting; the queue then waits for the preferred class."""
    classes = gpu_classes(model)
    images = class_images(model) or {}
    allowed = [c for c in classes if c in images or "default" in images]
    if not allowed:
        allowed = classes
    try:
        params = {"profile": f"prefer-{classes[0]}" if classes else "default", "gpus": max(int(gpus), 1),
                  "classes": ",".join(allowed), "regions": ",".join(model.get("regions") or [])}
        if region:
            params["pin"] = region
        r = httpx.get(f"{DISPATCHER_URL}/v1/rank", params=params, timeout=RANK_TIMEOUT_S)
        r.raise_for_status()
        best = (r.json() or {}).get("best")
        if best and best.get("gpu_class") in allowed:
            how = (f"dispatcher ({best.get('region')}/{best.get('pool')}, {best.get('capacity')}, "
                   f"{'free' if best.get('free') else 'queued'}, ${best.get('price')}/GPU-h)")
            return best["gpu_class"], how
    except Exception as e:  # noqa: BLE001 - the ranking is advisory; a submission must not fail because of it
        log.warning("dispatcher ranking unavailable (%s); using the preferred class", e)
    for c in allowed:
        if available is None or c in available:
            return c, "preferred (no ranking)"
    return allowed[0], "preferred (no ranking)"
