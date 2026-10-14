"""Endpoint children of a run (docs/JOBS.md "Endpoint children"): an operator-allowlisted, bounded primitive by which a
LIVE attempt of a run asks the platform to admit an asynchronous endpoint call as a child of its root, without any
customer credential in the worker, without broad storage credentials, and without an application scheduler.

The parent authenticates as its own pod (a projected ServiceAccount token the render gives main when the run class
declares `child_endpoints`; TokenReview on the parent's cluster: namespace, pod name and exact pod UID), and must be a
live attempt of the named run (its pod among the run's pods, by UID; the run not finished, not cancelled; the root's
reservation open). The allow-list and the bound are the ones FROZEN on the parent's Job at its render. The child
inherits the root's owner, namespace and key BY HASH: its caller pod (a platform pod, as every endpoint call's) carries
only the operation-scoped admission credential (`Bearer s2-admitted.<token>`, the HMAC of the root's key hash, the child
model and the child operation), which the gateway hook accepts only while the child holds a live reservation; no
customer or admin key is stored or forwarded anywhere. It inherits the parent's remaining deadline.

Identity: deterministic per (root, unit): `<root>-c-<sha256(unit)[:10]>`; the execution request (model, the input or
the input reference's sha256, the requested timeout and region) is bound by an immutable sha256; a replay with the same
unit and request reads the existing child back (any live attempt of the root, a successor included), a different
request for the same unit is refused; renewable transfer URLs are never part of the bound request. The child model's
image is recorded at admission: a pending intent is not completed against another image.

Capture: the child is rendered with the native application-I/O protocol of endpoint calls (docs/API.md "Endpoint
calls", services/jobs/bin/call.sh): readiness probe, ONE inference POST per operation (the durable posted marker: a
lost response is an unknown outcome, never a second POST), the response bytes and their manifest (sha256, media type,
size, status) made durable on the retained work volume before delivery, delivery to a platform-owned destination
`s3://<tenant bucket>/operations/<child>/{response.bin,call.json}` through presigned URLs the API itself mints for the
child on demand (`POST /internal/operations/<child>:refresh`, the child's own operation-scoped token: the API is the
child's refresh broker), transfer-only retries by the owner (`:export`). The request reaches the caller through the
child's private binding (inline `request.json`, or an `input_url` the parent supplies and may renew, verified against
the bound `input_sha256`); nothing of it enters the Job object.

Readback (`GET …/children/<unit>`, and every replay) is authoritative past the Job's TTL: the durable row, the capture
manifest read from the destination (sha256, media type, size, HTTP status, producing attempt), a presigned download
URL for the live attempt of the root (attempt-scoped, one object, no tenant credentials), the attempt records.

Lifecycle (durable intent, deterministic completion): the row is written FIRST, `pending`, with everything needed to
finish it (namespace, resolved region, effective timeout, the bound request, the transport); completion = the ledger
member (idempotent), the binding, then the caller Job (found, or created; AlreadyExists is the same Job) and the row
turns `active`. A process that dies in between, or an ambiguous create (the answer lost: the Job may exist), leaves the
row pending with its identity: the next replay of the same request and the reconciler's sweep (every 15 s) finish it;
nothing is rolled back unless the absence of the Job is confirmed and the refusal definite. `active` lasts until the
child's TERMINAL SETTLEMENT (its run member settled by billing, joined with the worker's native lease; a never-started
child settles at its deletion): a cancel (the cancel route, or the sweep's cascade for a cancelled, finished-without-
successor or gone parent) is recorded on the row and issued to the Job, and the row stays open, so no next child starts
while the old caller or worker may still be draining. Accounting on the root's ledger (keys.prepare_run_member kind
"child"): one request each; the root closes only when nothing is active."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

import urllib3
from fastapi import HTTPException
from kubernetes.client.rest import ApiException

import catalog
import db
import jobs
import keys
import kube
import status
from config import LABEL, PUBLIC_API_URL, REGION, TENANT_NS_PREFIX
from resilience import retry

log = logging.getLogger("api")

MIN_REMAINING_S = 60  # a child needs at least this much of the parent's deadline left
UNIT_RE = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
SHA_RE = r"[0-9a-f]{64}"
MAX_INLINE_INPUT_BYTES = 48 * 1024  # inline input travels in the child's private binding (64 KiB with its other keys)
URL_EXPIRES_S = 3600
OPEN = ("pending", "active")


def enabled() -> bool:
    return db.enabled()


def child_id(root: str, unit_id: str, incarnation: int = 1) -> str:
    """The child operation of a unit: deterministic per (root, unit); a retry of the unit (docs/JOBS.md "Endpoint
    children", unit retry) is the next incarnation, `-r<n>`: a new Job, binding and ledger member for the same unit."""
    base = f"{root}-c-{hashlib.sha256(unit_id.encode()).hexdigest()[:10]}"
    return base if incarnation <= 1 else f"{base}-r{incarnation}"


def _delivered(row: dict) -> bool:
    """Whether the unit's current child delivered its capture (the one inference per unit happened): an ended unit
    with a capture is final and replays; one without may be retried by a later parent of the same root."""
    import attempts

    recs = attempts.for_operation(row["child"]) if attempts.enabled() else []
    if (row.get("delivery") or "platform") == "application":
        return any(r.get("capture") for r in recs)
    try:
        return capture_of(row) is not None
    except HTTPException:
        return False


def binding_ref(child: str) -> str:
    """The child's private binding (execution.REF: a DNS label): the child id when it is one, else a digest of it."""
    import execution

    return child if execution.REF.match(child) else f"c-{hashlib.sha256(child.encode()).hexdigest()[:40]}"


def bound_request(
    model: str,
    inp: dict | None,
    input_ref: dict | None,
    timeout_s: int | None,
    region: str | None,
    result_ref: str | None = None,
) -> dict:
    """The immutable execution request of a unit: the model, the input (inline) or the input reference (its sha256),
    and the requested settings. Transfer URLs are renewable and never part of it; the effective timeout (capped by the
    parent's deadline at admission) is recorded on the row."""
    req = {"model": model, "timeout_s": int(timeout_s) if timeout_s else None, "region": region or None}
    if input_ref is not None:
        req["input_ref"] = {"sha256": input_ref["sha256"], "size_bytes": input_ref.get("size_bytes")}
    else:
        req["input"] = inp
    if result_ref:
        req["result_ref"] = result_ref  # application delivery: the application's immutable reference is bound too
    return req


def request_hash(req: dict) -> str:
    return hashlib.sha256(json.dumps(req, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def verify_parent_token(token: str) -> dict:
    """TokenReview of the parent attempt's projected token against the clusters this API reaches: {region, namespace,
    pod, uid}; 401 unless a bound pod token of a tenant namespace."""
    from kubernetes import client

    for region in sorted(kube.regions(), key=lambda r: r == REGION):
        try:
            review = retry(
                client.AuthenticationV1Api(kube._client(region)).create_token_review,
                client.V1TokenReview(spec=client.V1TokenReviewSpec(token=token)),
            )
        except (ApiException, OSError) as e:
            log.warning("token review in %s: %s", region, str(e)[:120])
            continue
        st = review.status
        if not st or not st.authenticated:
            continue
        parts = (st.user.username or "").split(":")
        if (
            len(parts) != 4
            or parts[0] != "system"
            or parts[1] != "serviceaccount"
            or not parts[2].startswith(TENANT_NS_PREFIX)
        ):
            raise HTTPException(401, "not a tenant run identity")
        extra = st.user.extra or {}
        pod = (extra.get("authentication.kubernetes.io/pod-name") or [""])[0]
        uid = (extra.get("authentication.kubernetes.io/pod-uid") or [""])[0]
        if not pod or not uid:
            raise HTTPException(401, "token is not bound to a pod")
        return {"region": region, "namespace": parts[2], "pod": pod, "uid": uid}
    raise HTTPException(401, "token not recognised by any cluster of this fleet")


def _live_parent(identity: dict, parent: str) -> tuple[dict, str, str, dict]:
    """The parent run `parent` as a live attempt of the authenticated pod: (job, region, root, class model). 409 for a
    stale/cancelled/finished attempt or a pod that is not the run's, 404 for an unknown run."""
    ns = identity["namespace"]
    job, region = jobs.find(ns, parent)
    md = job["metadata"]
    ann = md.get("annotations") or {}
    if (md.get("labels") or {}).get(f"{LABEL}/mode") != "run":
        raise HTTPException(409, f"operation {parent} is not a run")
    if status.finished(job) or ann.get(f"{LABEL}/cancelled") or ann.get(f"{LABEL}/successor"):
        raise HTTPException(409, f"operation {parent} is over or superseded; no child attaches to it")
    import attempts

    marks = attempts._marks(parent) if attempts.enabled() else None
    if marks and marks[1]:
        raise HTTPException(409, f"operation {parent} is cancelled; no child attaches to it")
    pods = job.get("_pods") or []
    mine = next((p for p in pods if p["metadata"].get("name") == identity["pod"]), None)
    if mine is None or mine["metadata"].get("uid") != identity["uid"]:
        raise HTTPException(
            409, f"pod {identity['pod']} is not a live attempt of {parent} (stale attempt or another run)"
        )
    if (mine.get("status") or {}).get("phase") not in ("Running", "Pending"):
        raise HTTPException(409, f"attempt {identity['pod']} of {parent} is not running")
    root = ann.get(f"{LABEL}/resumed-from") or parent
    m = catalog.require((md.get("labels") or {}).get(f"{LABEL}/model", ""))
    return job, region, root, m


def _frozen_allowlist(job: dict, m: dict) -> tuple[list[str], int]:
    """The child endpoints and the bound the parent was RENDERED with (its annotations), never the class as it is
    now: a changed class does not widen or move a running run's grant, and a run rendered without the grant (no
    annotation) has none, whatever its class declares today."""
    ann = job["metadata"].get("annotations") or {}
    if f"{LABEL}/child-endpoints" not in ann:
        return [], 0
    allowed = [x for x in ann[f"{LABEL}/child-endpoints"].split(",") if x]
    return allowed, int(ann.get(f"{LABEL}/child-max-units") or 0)


def _frozen_delivery(job: dict) -> str:
    """The delivery mode the parent was RENDERED with (`child-delivery` annotation): `platform` (the tenant bucket, the
    API's own refresh broker) or `application` (the parent's broker: external storage, docs/JOBS.md)."""
    return str((job["metadata"].get("annotations") or {}).get(f"{LABEL}/child-delivery") or "platform")


def preflight_parent(ns: str, m: dict) -> None:
    """At the PARENT's admission (`:invoke` of a class with `child_endpoints`), before anything is admitted: with
    `child_delivery: platform` every allowlisted child endpoint's region must hold the tenant's capture backend (the
    tenant bucket: children run where the endpoint is, not where the parent lands), else 400 naming region, model and
    the fix; with `application` nothing is probed or created (external storage: the parent's broker delivers)."""
    import artifacts

    jb = m.get("job") or {}
    if not jb.get("childEndpoints"):
        return
    if str(jb.get("childDelivery") or "platform") == "application":
        return
    for cid in jb["childEndpoints"]:
        cm = catalog.get(cid)
        if cm is None:
            raise HTTPException(400, f"child endpoint {cid} of {m['name']} is not in the catalog")
        for region in cm.get("regions") or [REGION]:
            try:
                artifacts.storage(ns, kube.cluster_region(region) or region)
            except HTTPException as e:
                raise HTTPException(
                    400,
                    f"class {m['name']}: child endpoint {cid} runs in {region}, where tenant {ns[len(TENANT_NS_PREFIX):]} has "
                    f"no capture backend ({e.detail}); with external storage declare child_delivery: application and have the "
                    "parent pass each child's delivery (transport.refresh_url + refresh_token, or response_url + call_url) "
                    "and an immutable result_ref",
                )


def _remaining_s(job: dict) -> int | None:
    """Seconds left of the parent's enforced deadline (activeDeadlineSeconds from its start), None without one."""
    spec = job.get("spec") or {}
    dl = spec.get("activeDeadlineSeconds")
    if not dl or int(dl) <= 1:
        return None
    start = (job.get("status") or {}).get("startTime") or job["metadata"].get("creationTimestamp")
    if not start:
        return int(dl)
    t0 = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
    return int(int(dl) - (datetime.now(timezone.utc) - t0).total_seconds())


_ROW = (
    "root, unit_id, child, parent, parent_pod, parent_pod_uid, model, request_sha256, state, created_at, ended_at, "
    "namespace, region, timeout_s, request, cancel_requested_at, reason, transport, model_image, binding_ref, delivery, "
    "incarnation, previous_child"
)


def _row(r) -> dict:
    return dict(zip([k.strip() for k in _ROW.split(",")], r, strict=True))


def _get(root: str, unit_id: str) -> dict | None:
    with db.pool().connection() as c:
        r = c.execute(
            f"select {_ROW} from endpoint_children where root = %s and unit_id = %s", (root, unit_id)
        ).fetchone()
    return _row(r) if r else None


def by_child(child: str) -> dict | None:
    with db.pool().connection() as c:
        r = c.execute(f"select {_ROW} from endpoint_children where child = %s", (child,)).fetchone()
    return _row(r) if r else None


def _ts(t) -> str | None:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def result_ref(ns: str, region: str, child: str) -> str:
    import artifacts

    return f"s3://{artifacts.storage(ns, region)['bucket']}/operations/{child}/response.bin"


def capture_of(row: dict) -> dict | None:
    """The authoritative capture of a child: the manifest the caller delivered next to the bytes (call.json is PUT only
    after response.bin was), read from the platform-owned destination; None until delivered. Survives the Job."""
    import artifacts

    ns, child = row["namespace"], row["child"]
    try:
        ref = result_ref(ns, row["region"], child)
        m = artifacts.read_json(ns, f"operations/{child}/call.json", region=row["region"], prefix=ref)
    except HTTPException:
        return None
    if not isinstance(m, dict) or not m.get("capture_complete"):
        return None
    out = {
        "uri": ref,
        "sha256": m.get("sha256"),
        "content_type": m.get("content_type"),
        "size_bytes": m.get("size_bytes"),
        "http_status": m.get("http_status"),
        "attempt_id": m.get("attempt_id"),
        "timings": m.get("timings"),
        "delivered": True,
    }
    try:
        d = artifacts.presign_download(ns, ref, URL_EXPIRES_S, region=row["region"])
        out["download_url"], out["download_expires_in_s"] = d["url"], URL_EXPIRES_S
    except HTTPException as e:
        out["download_error"] = e.detail
    return out


def readback(ns: str, root: str, unit_id: str) -> dict | None:
    row = _get(root, unit_id)
    if row is None:
        return None
    import attempts

    out = {
        "root": root,
        "unit_id": unit_id,
        "child": row["child"],
        "parent": row["parent"],
        "parent_pod": row["parent_pod"],
        "model": row["model"],
        "model_image": row["model_image"],
        "request_sha256": row["request_sha256"],
        "request": row["request"],
        "state": row["state"],
        "timeout_s": row["timeout_s"],
        "region": row["region"],
        "cancel_requested_at": _ts(row["cancel_requested_at"]),
        "reason": row["reason"],
        "created_at": _ts(row["created_at"]),
        "ended_at": _ts(row["ended_at"]),
        "incarnation": int(row.get("incarnation") or 1),
        "previous_child": row.get("previous_child"),
        "result_ref": None,
        "capture": None,
        "attempt": None,
        "status": None,
        "outcome": "pending",
    }
    out["delivery"] = row.get("delivery") or "platform"
    recs = attempts.for_operation(row["child"]) if attempts.enabled() and row["state"] != "pending" else []
    if row["state"] != "pending":
        if out["delivery"] == "application":
            # the application's reference and the capture the platform's uploader recorded (durable past the Job; no
            # platform-owned object, no download URL: the live parent fetches the exact object through its own broker)
            out["result_ref"] = (row["request"] or {}).get("result_ref")
            cap = next((r.get("capture") for r in reversed(recs) if r.get("capture")), None)
            out["capture"] = {**cap, "uri": out["result_ref"], "delivered": True} if cap else None
        else:
            try:
                out["result_ref"] = result_ref(row["namespace"] or ns, row["region"], row["child"])
            except HTTPException:
                pass
            out["capture"] = capture_of(row)
        if recs:
            a = recs[-1]
            out["attempt"] = {
                k: a.get(k)
                for k in ("pod", "epoch", "status", "exit_code", "export_state", "result_ref", "image_digest")
            }
    # the authoritative retry verdict for a caller (docs/JOBS.md "Endpoint children", unit retry): the unit ended
    # without its one inference; a LATER parent of the root may POST the same bound request with `retry: true`
    out["can_retry"] = bool(row["state"] == "ended" and out["capture"] is None and _never_inferred(recs))
    try:
        job, region = jobs.find(row["namespace"] or ns, row["child"])
        op = jobs.normalise(job)
        out.update({"status": op["status"], "error": op.get("error"), "ended": op.get("ended_at")})
    except HTTPException as e:
        if e.status_code != 404:
            out["status"] = "UNAVAILABLE"
        elif row["state"] == "pending":
            out["status"] = "PENDING"
        elif out["attempt"] and out["attempt"]["status"]:
            out["status"] = str(out["attempt"]["status"]).upper()  # the record outlives the Job
        else:
            out["status"] = "ENDED" if row["state"] == "ended" else "UNKNOWN"
    # the outcome the parent may act on: a delivered capture decides (its HTTP status); without one, the producing
    # attempt's exit says whether a capture exists to deliver again (0/2/5 with a failed export: `:export` by the
    # owner), or whether the inference outcome is unknown for good (4: posted, no response; 3: nothing captured),
    # never to be inferred again; otherwise an ended child failed (refused, cancelled, lost) and an open one pends
    cap, att = out["capture"], out["attempt"]
    exit_code = (att or {}).get("exit_code")
    terminal = bool(att) and str(att.get("status")).lower() in ("failed", "succeeded", "interrupted")
    out["exportable"] = False
    if cap:
        st = int(cap.get("http_status") or 0)
        out["outcome"] = "succeeded" if 200 <= st < 300 else "failed"
    elif terminal and exit_code in (3, 4):
        out["outcome"] = "unknown"  # no capture exists: not exportable, not to be inferred again under this unit
    elif terminal and exit_code in (0, 2, 5) and att.get("export_state") == "failed":
        out["outcome"], out["exportable"] = "undelivered", True  # a complete capture on the volume: :export
    elif row["state"] == "ended":
        out["outcome"] = "failed"  # over without a delivered capture: refused, cancelled, lost
    return out


def _input_of(body: dict) -> tuple[dict | None, dict | None, dict]:
    """(inline input, input reference, transport) from the request body; 400 unless exactly one of input/input_ref."""
    inp, ref, tr = body.get("input"), body.get("input_ref"), body.get("transport") or {}
    if not isinstance(tr, dict) or any(not isinstance(v, str) for v in tr.values()):
        raise HTTPException(400, "transport must map names to strings")
    if (inp is None) == (ref is None):
        raise HTTPException(400, "give exactly one of input (inline object) or input_ref ({sha256, size_bytes?})")
    if inp is not None:
        if not isinstance(inp, dict):
            raise HTTPException(400, "input must be an object")
        if len(json.dumps(inp, separators=(",", ":")).encode()) > MAX_INLINE_INPUT_BYTES:
            raise HTTPException(
                400, f"inline input above {MAX_INLINE_INPUT_BYTES} bytes: give input_ref + transport.input_url instead"
            )
        return inp, None, tr
    import re

    if not isinstance(ref, dict) or not re.fullmatch(SHA_RE, str(ref.get("sha256") or "")):
        raise HTTPException(400, "input_ref needs sha256 (64 hex digits) of the exact request body the endpoint gets")
    size = ref.get("size_bytes")
    if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size < 0):
        raise HTTPException(400, "input_ref.size_bytes must be a non-negative integer")
    if not str(tr.get("input_url") or "").startswith("https://"):
        raise HTTPException(400, "input_ref needs transport.input_url (https) the caller fetches the request from")
    return None, {"sha256": ref["sha256"], "size_bytes": size}, tr


def _not_inferred(row: dict) -> bool:
    """Whether the unit's current child definitely never ran its one inference: no attempt at all (never started, or
    cancelled before its pod ran), or its last attempt ended before the inference POST (call.sh exit 2: refused
    before/at readiness, a definite 4xx with no capture; exit 3: nothing captured, transfer-only) with no capture on
    record. Never an outcome-unknown attempt (exit 4: the POST went out, the answer was lost) and never a capture."""
    import attempts

    recs = attempts.for_operation(row["child"]) if attempts.enabled() else []
    return _never_inferred(recs)


def _never_inferred(recs: list) -> bool:
    """From ALL attempt records of the child's lineage (the child and its transfer-only `-x<n>` retries): no attempt
    at all (never started, or cancelled before its pod ran), or every attempt ended before the inference POST
    (call.sh exit 2 with no capture manifest: refused before/at readiness; a 4xx answer to the POST itself carries a
    manifest and is a capture). One attempt with exit 3 (nothing captured, transfer-only), exit 4 (posted, outcome
    unknown), no exit on record, or a capture (0/2/5: the inference happened, delivered or not) makes the whole
    history "maybe inferred": a later exit 2 (an export retry refused at its input refresh) never erases an earlier
    uncertainty."""
    return all(not r.get("capture") and r.get("exit_code") == 2 for r in recs)


def create(identity: dict, parent: str, body: dict) -> tuple[dict, bool]:
    """Admit one child (see the module docstring). Returns (readback, created)."""
    unit_id = str(body.get("unit_id") or "")
    import re

    if not re.fullmatch(UNIT_RE, unit_id):
        raise HTTPException(400, "unit_id: 1..128 of [A-Za-z0-9._:-], starting alphanumeric")
    model = str(body.get("model") or "")
    inp, input_ref, transport = _input_of(body)
    req_timeout = body.get("timeout_s")
    if req_timeout is not None and (
        not isinstance(req_timeout, int) or isinstance(req_timeout, bool) or req_timeout < 1
    ):
        raise HTTPException(400, "timeout_s must be a positive integer")
    job, region, root, m = _live_parent(identity, parent)
    ns = identity["namespace"]
    allowed, max_units = _frozen_allowlist(job, m)
    delivery = _frozen_delivery(job)
    app_result_ref = None
    if delivery == "application":
        # the parent's broker delivers (external storage): each child request carries its application delivery (the
        # renewable broker, or static URLs) and the application's immutable reference for the captured object
        app_result_ref = str(body.get("result_ref") or "")
        if not app_result_ref or len(app_result_ref) > 512:
            raise HTTPException(
                400, "result_ref: the application's immutable reference for the captured object (1..512 chars)"
            )
        has_broker = str(transport.get("refresh_url") or "").startswith("https://") and bool(
            transport.get("refresh_token")
        )
        has_static = str(transport.get("response_url") or "").startswith("https://") and str(
            transport.get("call_url") or ""
        ).startswith("https://")
        if not has_broker and not has_static:
            raise HTTPException(
                400,
                "application delivery: transport needs refresh_url (https) + refresh_token (the application's broker, "
                "call.sh's refresh contract), or static response_url + call_url (https)",
            )
    if not allowed:
        raise HTTPException(403, f"run class {m['name']} declares no child endpoints")
    if model not in allowed:
        raise HTTPException(403, f"model {model} is not a child endpoint of {m['name']} ({allowed})")
    cm = catalog.get(model)
    if cm is None or cm.get("mode") == "run":
        raise HTTPException(400, f"child model {model} is not an endpoint")
    req = bound_request(model, inp, input_ref, req_timeout, body.get("region"), app_result_ref)
    h = request_hash(req)
    child = child_id(root, unit_id)
    ann = job["metadata"].get("annotations") or {}
    key_hash = ann.get(f"{LABEL}/key")
    tenant = (job["metadata"].get("labels") or {}).get(f"{LABEL}/tenant")
    if not key_hash or not tenant:
        raise HTTPException(409, f"run {parent} carries no key on record; no child attaches to it")
    if not ann.get(f"{LABEL}/owner"):
        raise HTTPException(409, f"run {parent} carries no owner on record; no child attaches to it")
    ki = keys.info(key_hash)
    if ki is None:
        raise HTTPException(409, f"run {parent}: its key no longer exists; no child attaches to it")
    import app as api

    # a child is an endpoint call of the root's key: the same policy as any endpoint call (a key with a finite
    # GPU-second budget is refused with 501 there too: shared endpoint GPU time is not metered per call)
    api._endpoint_limits(api.Principal(key="", tenant=tenant, info=ki, key_hash=key_hash), "async")
    if not api.cfg.ROUTER_KEY:
        raise HTTPException(503, "endpoint children need the fleet's admission secret (ROUTER_KEY)")
    if not PUBLIC_API_URL:
        raise HTTPException(503, "endpoint children need PUBLIC_API_URL (the child's refresh broker is this API)")
    _cluster, child_region = api._deployment_of(cm, req["region"])  # definite (400/404) before anything is recorded
    if delivery == "platform":
        result_ref(ns, child_region, child)  # the tenant's storage in that region exists (404/503 before any record)
    image = str((cm.get("runtime") or {}).get("image") or "")
    # the durable intent first, under the one-open-child and per-unit identities (compare-and-swap on the primary key)
    with db.pool().connection() as c, c.transaction():
        c.execute("select pg_advisory_xact_lock(hashtextextended(%s, 3))", (root,))
        existing = c.execute(
            "select child, request_sha256, state, incarnation from endpoint_children where root = %s and unit_id = %s",
            (root, unit_id),
        ).fetchone()
        retry = False
        if existing:
            if existing[1] != h:
                raise HTTPException(
                    409,
                    f"unit {unit_id} of {root} already has a child with a different request ({existing[0]}: the model, "
                    "input or input_ref.sha256, timeout_s and region of a unit are bound at its first admission)",
                )
            pending = existing[2] == "pending"
            if transport and existing[2] in OPEN:  # renewed transfer URLs for the same bound request
                c.execute(
                    "update endpoint_children set transport = %s where root = %s and unit_id = %s",
                    (json.dumps(transport), root, unit_id),
                )
            if existing[2] == "ended" and body.get("retry") is True:
                # unit retry, explicit (docs/JOBS.md "Endpoint children"): the caller asks to run the unit again. Only a
                # LATER parent of the root (an owner's continuation, not the parent that owned the ended child) may, with
                # the same bound request, and only for a child that definitely never inferred (no capture, no
                # outcome-unknown attempt): the unit reopens as the next incarnation (a new Job, binding and ledger
                # member; the earlier child's Job and records stay as history). Without `retry` the ended row replays.
                row_now = _get(root, unit_id)
                if _delivered(row_now):
                    raise HTTPException(
                        409, f"unit {unit_id} of {root} delivered its capture ({existing[0]}); a unit infers once"
                    )
                if parent == row_now["parent"] or not ann.get(f"{LABEL}/resumed-from"):
                    raise HTTPException(
                        409,
                        f"unit {unit_id} of {root}: only a later continuation of the root (an owner's resume) retries a "
                        f"unit; {parent} is the parent that owned {existing[0]}",
                    )
                if not _not_inferred(row_now):
                    raise HTTPException(
                        409,
                        f"unit {unit_id} of {root}: the outcome of {existing[0]} is unknown (its inference was posted); "
                        "it is not retried",
                    )
                retry = True
        if retry or not existing:
            active = c.execute(
                "select child, unit_id, state from endpoint_children where root = %s and state in ('pending', 'active')",
                (root,),
            ).fetchone()
            if active:
                raise HTTPException(
                    409,
                    f"run {root}: child {active[0]} (unit {active[1]}) is still {active[2]}; one child at a time, "
                    "until its terminal settlement",
                )
            remaining = _remaining_s(job)
            if remaining is not None and remaining < MIN_REMAINING_S:
                raise HTTPException(409, f"run {parent}: {remaining} s of its deadline left; no child attaches")
            timeout = int(req_timeout or int((cm.get("runtime") or {}).get("timeout") or 600))
            if remaining is not None:
                timeout = min(timeout, remaining)
            if timeout < MIN_REMAINING_S:
                raise HTTPException(400, f"timeout_s must leave at least {MIN_REMAINING_S} s")
        if retry:
            incarnation = int(existing[3] or 1) + 1
            child = child_id(root, unit_id, incarnation)
            c.execute(
                "update endpoint_children set child = %s, parent = %s, parent_pod = %s, parent_pod_uid = %s, "
                "state = 'pending', ended_at = null, reason = null, cancel_requested_at = null, namespace = %s, "
                "region = %s, timeout_s = %s, transport = %s, model_image = %s, binding_ref = %s, incarnation = %s, "
                "previous_child = %s where root = %s and unit_id = %s and state = 'ended'",
                (
                    child,
                    parent,
                    identity["pod"],
                    identity["uid"],
                    ns,
                    child_region,
                    timeout,
                    json.dumps(transport),
                    image,
                    binding_ref(child),
                    incarnation,
                    existing[0],
                    root,
                    unit_id,
                ),
            )
            pending = True
        elif not existing:
            n = c.execute("select count(*) from endpoint_children where root = %s", (root,)).fetchone()[0]
            if n >= max_units:
                raise HTTPException(409, f"run {root}: {max_units} children already (child_max_units of {m['name']})")
            c.execute(
                "insert into endpoint_children (root, unit_id, child, parent, parent_pod, parent_pod_uid, model, request_sha256, state, namespace, region, timeout_s, request, transport, model_image, binding_ref, delivery) values (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    root,
                    unit_id,
                    child,
                    parent,
                    identity["pod"],
                    identity["uid"],
                    model,
                    h,
                    ns,
                    child_region,
                    timeout,
                    json.dumps(req),
                    json.dumps(transport),
                    image,
                    binding_ref(child),
                    delivery,
                ),
            )
            pending = True
    if not pending:
        return readback(ns, root, unit_id), False  # replay / readback after a lost response
    row = _get(root, unit_id)
    complete(row, parent_ann=ann, tenant=tenant, key_hash=key_hash, synchronous=True)
    return readback(ns, root, unit_id), retry or not bool(existing)


def _binding_data(row: dict, admitted: str) -> dict[str, str]:
    """The child's private binding (docs/API.md "Endpoint calls"): the request (inline) or its bound sha256 (fetched
    through the renewable input_url), the stable result reference, and the delivery: `platform` = this API as the
    refresh broker with the child's own operation-scoped token and a platform-owned destination; `application` = the
    parent's broker (its refresh_url/refresh_token: the same call.sh refresh contract, the child's projected identity
    token on every call when the parent's execution had one) or the application's static URLs, and the application's
    own immutable result_ref. No customer key, no storage credential, in either mode."""
    req, tr = row["request"], row.get("transport") or {}
    if row.get("delivery") == "application":
        data = {"result_ref": str(req.get("result_ref") or "")}
        if tr.get("refresh_url"):
            data["refresh_url"], data["refresh_token"] = tr["refresh_url"], tr.get("refresh_token") or ""
        else:
            data["response_url"], data["call_url"] = tr.get("response_url") or "", tr.get("call_url") or ""
        if tr.get("input_url"):
            data["input_url"] = tr["input_url"]
    else:
        data = {
            "refresh_url": f"{PUBLIC_API_URL}/internal/operations/{row['child']}:refresh",
            "refresh_token": admitted,
            "result_ref": result_ref(row["namespace"], row["region"], row["child"]),
        }
    if req.get("input_ref"):
        data["input_sha256"] = req["input_ref"]["sha256"]
    else:
        data["request.json"] = json.dumps(req.get("input") or {}, separators=(",", ":"))
    return data


def complete(
    row: dict,
    parent_ann: dict | None = None,
    tenant: str | None = None,
    key_hash: str | None = None,
    synchronous: bool = False,
) -> str:
    """Finish a pending intent deterministically, from the row alone: the ledger member (idempotent), the binding,
    then the caller Job (found, or created; AlreadyExists is the same Job), then the row `active`. Returns the row's
    state afterwards. Definite refusals (the ledger's 4xx, the API server's definite 4xx with no Job, a changed child
    image) roll the intent back: synchronously they raise (nothing recorded), from the sweep the row ends with the
    reason. Anything ambiguous (transport errors, 5xx: the Job may exist) keeps the row pending with its member;
    synchronously that is a 503 telling the caller to replay the same request, which reads the intent back and
    finishes it."""
    root, unit_id, child, ns = row["root"], row["unit_id"], row["child"], row["namespace"]
    import app as api
    import execution
    import sessions

    cm = catalog.get(row["model"])
    if cm is None:
        return _refuse(row, f"child model {row['model']} is gone from the catalog", 400, synchronous)
    image = str((cm.get("runtime") or {}).get("image") or "")
    if row["model_image"] and image != row["model_image"]:
        return _refuse(
            row,
            f"child model {row['model']} changed its image since the admission ({row['model_image']} -> {image}); "
            "the intent is not completed against another image",
            409,
            synchronous,
        )
    if parent_ann is None or tenant is None or key_hash is None:
        try:
            pjob, _ = jobs.find(ns, row["parent"])
        except HTTPException as e:
            if e.status_code != 404:
                raise
            return _refuse(row, f"parent {row['parent']} is gone before the child was created", 409, synchronous)
        parent_ann = pjob["metadata"].get("annotations") or {}
        tenant = (pjob["metadata"].get("labels") or {}).get(f"{LABEL}/tenant")
        key_hash = parent_ann.get(f"{LABEL}/key")
        if not tenant or not key_hash:
            return _refuse(row, f"parent {row['parent']} carries no key on record", 409, synchronous)
    owner = parent_ann.get(f"{LABEL}/owner")
    if not owner:
        return _refuse(row, f"parent {row['parent']} carries no owner on record", 409, synchronous)
    with db.pool().connection() as c, c.transaction():
        c.execute("select pg_advisory_xact_lock(hashtextextended(%s, 3))", (root,))
        st = c.execute(
            "select state from endpoint_children where root = %s and unit_id = %s", (root, unit_id)
        ).fetchone()
        if st is None or st[0] != "pending":
            return st[0] if st else "gone"  # finished (or ended) by another process under the lock
        try:
            keys.prepare_run_member(
                root, child, None, sessions.gpus_per_replica(cm), int(row["timeout_s"]), kind="child", stage=unit_id
            )
        except keys.KeyLimit as e:
            return _refuse(row, e.detail, e.status, synchronous)
        try:
            cjob, region = jobs.find(ns, child)
        except HTTPException as e:
            if e.status_code != 404:
                raise  # unreachable clusters: ambiguous, the intent stays pending
            cjob = None
        if cjob is None:
            admitted = api._admitted_token(key_hash, cm["name"], child)
            try:
                # the binding lives where the API's delivery reads it (the API's own cluster, as a caller's
                # PUT /v1/bindings/{ref}): jobs.create -> execution.deliver claims it there and copies it into the
                # child's region (`<child>-private`, next to the Job); written in the child's region instead, the
                # delivery never finds it (404, the intent rolled back, an orphan Secret in the region)
                execution.put_binding(
                    ns, tenant, row["binding_ref"], _binding_data(row, admitted), owner, False, None, REGION
                )
            except HTTPException as e:
                if e.status_code < 500 and e.status_code != 409:
                    keys.rollback_run_member(child)
                    return _refuse(row, f"the child's binding was refused: {e.detail}", e.status_code, synchronous)
                return _ambiguous(row, f"binding: {e.detail}", synchronous)
            prefix = (
                ""
                if row.get("delivery") == "application"
                else f"s3://{result_ref(ns, row['region'], child)[5:].split('/', 1)[0]}/operations/{child}"
            )
            # the parent's FROZEN execution (its Job's annotation, never the current tenant config): the child's caller
            # runs under the same workload identity (call.sh sends its own projected token to the broker) and the
            # same execution profile; the parent's placement/recovery/graph options do not apply to a call
            try:
                pex = json.loads(parent_ann.get(f"{LABEL}/execution") or "null") or {}
            except ValueError:
                pex = {}
            ex = {"io_mode": "application", "private_input_ref": row["binding_ref"]}
            for k in ("identity", "profile"):
                if pex.get(k):
                    ex[k] = pex[k]
            # the child's caller carries ONLY the operation-scoped credential: the admission token (HMAC of the root's
            # key hash, the child model and the child operation), presented as `Bearer s2-admitted.<token>`; the
            # gateway hook rebuilds the principal from the key record by hash and accepts it only while the child
            # holds a live reservation. No customer or admin key is stored or forwarded; the call goes through the
            # model's gateway (never LiteLLM's pass-through, which would need a key). Application I/O: the request
            # from the binding, one durable POST, the capture delivered to the platform-owned destination
            cjob, secret = jobs.build_async(
                child,
                {**cm, "litellm_route": None},
                {},
                tenant,
                f"{api.ADMITTED_BEARER_PREFIX}{admitted}",
                f"child:{unit_id}",
                None,
                int(row["timeout_s"]),
                prefix,
                row["region"],
                ex=ex,
                key_hash=key_hash,
                admitted=admitted,
            )
            a = cjob["metadata"]["annotations"]
            a[f"{LABEL}/child-of"] = row["parent"]
            a[f"{LABEL}/child-root"] = root
            a[f"{LABEL}/child-unit"] = unit_id
            a[f"{LABEL}/child-request-sha256"] = row["request_sha256"]
            jobs._inherit_owner(cjob, parent_ann)
            if ex.get("profile"):  # the parent's execution profile selects the child's network policy as well
                cjob["spec"]["template"].setdefault("metadata", {}).setdefault("labels", {})[
                    f"{LABEL}/execution-profile"
                ] = ex["profile"]["name"]
            try:
                jobs.create(ns, cjob, row["region"], secret)
            except HTTPException as e:
                if e.status_code < 500:
                    # definite: the API server refused the create, no Job exists (404 namespace, 409 is "exists with
                    # another request": impossible for a deterministic render, 4xx validation)
                    keys.rollback_run_member(child)
                    return _refuse(row, f"the child's Job was refused: {e.detail}", e.status_code, synchronous)
                return _ambiguous(row, str(e.detail), synchronous)
            except (ApiException, urllib3.exceptions.HTTPError, OSError) as e:
                return _ambiguous(row, f"{type(e).__name__}: {str(e)[:200]}", synchronous)
        c.execute(
            "update endpoint_children set state = 'active' where root = %s and unit_id = %s and state = 'pending'",
            (root, unit_id),
        )
    log.info("child %s of %s (root %s, unit %s) active on %s", child, row["parent"], root, unit_id, row["region"])
    return "active"


def _refuse(row: dict, why: str, code: int, synchronous: bool) -> str:
    """A definite refusal of a pending intent whose Job is confirmed absent: synchronously nothing stays recorded (the
    caller gets the error, the unit may be asked again); from the sweep the row ends with the reason (the readback
    shows it; the member, if any, was rolled back by the caller of this)."""
    if synchronous:
        _drop(row["root"], row["unit_id"])
        raise HTTPException(code, why)
    _end(row["root"], row["unit_id"], f"refused: {why}")
    return "ended"


def _ambiguous(row: dict, why: str, synchronous: bool) -> str:
    log.warning("child %s: creation did not confirm (%s); the intent stays pending", row["child"], why)
    if synchronous:
        raise HTTPException(
            503,
            f"child {row['child']}: the intent is recorded but its creation did not confirm ({why}); replay the same "
            "request to read it back (the sweep finishes it too)",
        )
    return "pending"


def _drop(root: str, unit_id: str) -> None:
    with db.pool().connection() as c, c.transaction():
        c.execute(
            "delete from endpoint_children where root = %s and unit_id = %s and state = 'pending'", (root, unit_id)
        )


def refresh(child: str, token: str, body: dict) -> dict:
    """The child's refresh broker (call.sh `refresh`): fresh transfer URLs for a LIVE attempt of the child, or of one
    of its transfer-only export retries (`<child>-x<n>`, the owner's `:export`: the same binding, the same token), that
    proves the child's own operation-scoped token: the parent-supplied input_url (renewable through a replay of the
    same request) for the input phase, presigned PUT URLs for the response bytes and the manifest at the platform-owned
    destination for the output phase. The operation named must hold a live reservation on the root's key (the child
    while it is open; a retry while it runs) and the attempt named must be one of its live pods: a stale pod, a settled
    operation or another model's token gets nothing (401/403/409) and delivers nothing."""
    import hmac
    import re

    import app as api
    import artifacts

    row = by_child(child)
    if row is None or not token:
        raise HTTPException(401, "no such child or no token")
    if row.get("delivery") == "application":
        raise HTTPException(
            409, f"child {child} delivers through the application's broker; this API mints nothing for it"
        )
    op_id = str(body.get("operation_id") or child)
    if op_id != child and not re.fullmatch(re.escape(child) + r"-x[0-9]+", op_id):
        raise HTTPException(400, f"operation_id must be the child {child} or one of its export retries")
    h = api._reservation_key_of(op_id)
    if not h or not hmac.compare_digest(token, api._admitted_token(h, row["model"], child)):
        raise HTTPException(401, "refresh refused (not the child's token, or no live reservation)")
    if op_id == child and row["state"] not in OPEN:
        raise HTTPException(409, f"child {child} is {row['state']}: nothing to transfer any more")
    attempt = str(body.get("attempt_id") or "")
    ns, region = row["namespace"], row["region"]
    try:
        job, _ = jobs.find(ns, op_id)
    except HTTPException as e:
        if e.status_code != 404:
            raise
        raise HTTPException(409, f"operation {op_id} has no Job any more: no live attempt to transfer for")
    if (job["metadata"].get("annotations") or {}).get(f"{LABEL}/key") != h or status.finished(job):
        raise HTTPException(409, f"operation {op_id} is over or not the child's")
    live = [
        x
        for x in (job.get("_pods") or [])
        if x["metadata"].get("name") == attempt and (x.get("status") or {}).get("phase") in ("Pending", "Running")
    ]
    if not attempt or not live:
        raise HTTPException(
            403, f"attempt {attempt or '?'} is not a live pod of {op_id}: nothing is transferred for it"
        )
    out: dict = {}
    if body.get("phase") == "input":
        url = (row["transport"] or {}).get("input_url")
        if url:
            out["input_url"] = url
        return out
    ref = result_ref(ns, region, child)
    out["response_url"] = artifacts.presign_put(ns, ref, URL_EXPIRES_S, region)["url"]
    out["call_url"] = artifacts.presign_put(ns, ref[: -len("response.bin")] + "call.json", URL_EXPIRES_S, region)["url"]
    return out


def _parent_over(ns: str, root: str, parent: str) -> str | None:
    """Why the child's parent lineage is over (the cascade's cause), None while a live attempt of the root may still
    read the child: cancelled (annotation or the attempt record), finished with no successor, gone (no Job, no attempt
    record); a finished parent with a successor is judged by the successor (the resumed attempt reads the child back)."""
    import attempts

    if attempts.enabled() and (marks := attempts._marks(root)) and marks[1]:
        return "root cancelled"
    name = parent
    for _ in range(16):
        try:
            pjob, _ = jobs.find(ns, name)
        except HTTPException as e:
            if e.status_code != 404:
                raise
            if attempts.enabled() and attempts.for_operation(root):
                return None  # the Job expired but the run is on record: nothing to judge from here
            return f"parent {name} gone"
        pann = pjob["metadata"].get("annotations") or {}
        if pann.get(f"{LABEL}/cancelled"):
            return f"parent {name} cancelled"
        succ = pann.get(f"{LABEL}/successor")
        if succ and succ != name:
            name = succ  # judged by the successor attempt
            continue
        if status.finished(pjob):
            return f"parent {name} finished without a successor"
        return None
    return None


def _settled(child: str) -> bool:
    m = keys.member_of(child)
    return m is not None and m["state"] != "active"


def sweep() -> list[str]:
    """Every 15 s, the durable lifecycle of every open child: a pending intent is completed (or ended when its parent
    is over before anything was created); an active child whose run member settled (billing, joined with the worker's
    lease) is recorded ended; an active child whose parent lineage is over gets its cancel issued once and recorded,
    and stays open until that settlement; a child whose Job is gone with nothing to settle is ended as lost. Returns
    `<child>:<what>`."""
    if not enabled():
        return []
    out = []
    with db.pool().connection() as c:
        rows = [
            _row(r)
            for r in c.execute(
                f"select {_ROW} from endpoint_children where state in ('pending', 'active') order by created_at"
            ).fetchall()
        ]
    for row in rows:
        child, root, unit_id, ns = row["child"], row["root"], row["unit_id"], row["namespace"]
        try:
            if row["state"] == "pending":
                over = _parent_over(ns, root, row["parent"])
                if over:
                    if keys.member_of(child):
                        try:
                            jobs.find(ns, child)
                        except HTTPException as e:
                            if e.status_code != 404:
                                raise
                            keys.rollback_run_member(child)  # confirmed absent: never created
                            _end(root, unit_id, f"{over} before the child was created")
                            out.append(f"{child}:ended")
                            continue
                        # the Job exists after all (created, the row not yet active): finish and cancel below
                    else:
                        _end(root, unit_id, f"{over} before the child was created")
                        out.append(f"{child}:ended")
                        continue
                state = complete(row)
                out.append(f"{child}:{state}")
                if state != "active":
                    continue
                row = _get(root, unit_id)
            if _settled(child):
                _end(root, unit_id, row["reason"] or "settled")
                out.append(f"{child}:ended")
                continue
            try:
                cjob, cregion = jobs.find(ns, child)
            except HTTPException as e:
                if e.status_code != 404:
                    raise
                if keys.member_of(child) is None or row["cancel_requested_at"] is not None:
                    _end(root, unit_id, row["reason"] or "lost")  # nothing to settle, nothing drains
                    out.append(f"{child}:ended")
                continue
            if row["cancel_requested_at"] is not None:
                continue  # issued; the settlement ends it
            over = _parent_over(ns, root, row["parent"])
            if over:
                _cancel(row, cjob, cregion, over)
                out.append(f"{child}:cancelled")
        except Exception:  # noqa: BLE001
            log.exception("child sweep: %s", child)
    return out


def _cancel(row: dict, cjob: dict, cregion: str, why: str) -> None:
    """Issue the child's cancel once and record it; the row stays open until the settlement (a never-started child,
    deleted by the cancel, settles at zero right here: nothing of it ever ran or attached)."""
    import app as api

    cancelled = jobs.cancel(row["namespace"], row["child"], cregion)
    if db.enabled():
        import leases

        try:
            leases.request_cancel(row["child"], why)  # a request to the attached worker, never a release
        except Exception as e:  # noqa: BLE001
            log.warning("child %s: cancel request to the worker: %s", row["child"], e)
    api.settle_deleted(cancelled)
    with db.pool().connection() as c, c.transaction():
        c.execute(
            "update endpoint_children set cancel_requested_at = now(), reason = coalesce(reason, %s) where root = %s and unit_id = %s and state in ('pending', 'active')",
            (f"cancelled: {why}", row["root"], row["unit_id"]),
        )
    if _settled(row["child"]):
        _end(row["root"], row["unit_id"], f"cancelled: {why}")


def cancel_for_root(ns: str, root: str, why: str = "root cancelled by its owner") -> list[str]:
    """The cancel route's immediate cascade (the sweep repeats it durably): every open child gets its cancel issued and
    recorded; the rows stay open until their settlement (a never-started child settles at once)."""
    out = []
    if not enabled():
        return out
    with db.pool().connection() as c:
        rows = [
            _row(r)
            for r in c.execute(
                f"select {_ROW} from endpoint_children where root = %s and state in ('pending', 'active')", (root,)
            ).fetchall()
        ]
    for row in rows:
        child, unit_id = row["child"], row["unit_id"]
        if row["cancel_requested_at"] is not None:
            continue
        try:
            cjob, cregion = jobs.find(ns, child)
        except HTTPException as e:
            if e.status_code != 404:
                raise
            if row["state"] == "pending":
                if keys.member_of(child):
                    keys.rollback_run_member(child)  # confirmed absent: never created
                _end(root, unit_id, f"{why} before the child was created")
            else:
                _end(root, unit_id, f"cancelled: {why} (job gone)")
            out.append(child)
            continue
        _cancel(row, cjob, cregion, why)
        out.append(child)
    return out


def _end(root: str, unit_id: str, reason: str | None = None) -> None:
    with db.pool().connection() as c, c.transaction():
        c.execute(
            "update endpoint_children set state = 'ended', ended_at = now(), reason = coalesce(reason, %s) where root = %s and unit_id = %s and state in ('pending', 'active')",
            (reason, root, unit_id),
        )
