"""API keys and the GPU charge ledger in the fleet database (docs/API.md "Keys").

The `keys` table is the source of truth for a key's tenant, role, budget, spend, model allow-list, expiry and
block flag; only the sha256 of a key's value is stored (the value is shown once, at creation). LiteLLM keeps a
mirror of every tenant key (created with the same value, updated and deleted best effort) so the same key works
on its OpenAI route; the API never reads the mirror back. `charges` is the idempotent GPU ledger billing.py
writes: one row per finished run, the key's spend incremented in the same transaction.

Roles: `user`, `admin` (key management, model definitions) and `router`: the per-fleet key LiteLLM's
deployments call the gateway with (ROUTER_KEY, Secret api/router-key), accepted by the gateway's authorize
hook only, every model, no budget, never billed.

CLI: `python -m keys bootstrap` (KEY = the value, KEY_SPEC = JSON {alias, tenant, role, budget, models, routes})
creates or updates one key straight in the database: the models stage runs it once per tenant key of
terraform.tfvars, before any admin key exists that could mint through the API.
"""
import hashlib, json, logging, os, secrets, string, sys, time
from datetime import datetime, timedelta, timezone
import db
from config import LITELLM_MASTER_KEY, LITELLM_URL

log = logging.getLogger("keys")
ROUTER = {"tenant": "platform", "alias": "router", "role": "router"}
COLS = "hash, alias, tenant, role, models, budget, spend, expires, blocked, preview, metadata, created_at, created_by, updated_at"
_ALNUM = string.ascii_letters + string.digits


def key_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_value() -> str:
    """`sk-` + 40 alphanumerics: the shape LiteLLM and the Terraform-generated keys have (safe in a WebSocket subprotocol)."""
    return "sk-" + "".join(secrets.choice(_ALNUM) for _ in range(40))


def preview(value: str) -> str:
    return "sk-..." + value[-4:]


def _iso(t) -> str | None:
    return t.isoformat() if t else None


def _when(s) -> datetime | None:
    """An ISO timestamp from LiteLLM (or None) as an aware datetime; psycopg needs a datetime for timestamptz."""
    if not s:
        return None
    t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _info(r) -> dict:
    """A key as the API's Principal carries it: the shape LiteLLM's /key/info returned, kept so that nothing above
    auth.py changed when the keys moved into the fleet database."""
    h, alias, tenant, role, models, budget, spend, expires, blocked, prev, meta, created, by, _updated = r
    return {"token": h, "key_name": prev, "key_alias": alias, "max_budget": float(budget) if budget is not None else None,
            "spend": float(spend or 0), "models": list(models or []), "expires": _iso(expires), "blocked": bool(blocked),
            "created_at": _iso(created), "created_by": by, "metadata": {"tenant": tenant, "role": role, **(meta or {})}}


def info(h: str) -> dict | None:
    with db.pool().connection() as c:
        r = c.execute(f"select {COLS} from keys where hash = %s", (h,)).fetchone()
        return _info(r) if r else None


def list_tenant(tenant: str) -> list[dict]:
    with db.pool().connection() as c:
        return [_info(r) for r in c.execute(f"select {COLS} from keys where tenant = %s order by alias", (tenant,)).fetchall()]


def _expiry(days: int | None) -> datetime | None:
    """days from now; 0 = never."""
    return datetime.now(timezone.utc) + timedelta(days=days) if days else None


def _conflict(e, alias: str):
    import psycopg
    if isinstance(e, psycopg.errors.UniqueViolation):
        from fastapi import HTTPException
        raise HTTPException(409, f"key {alias} exists") from None
    raise e


def create(tenant: str, alias: str, budget: float | None = None, models: list[str] | None = None, expires_days: int | None = None,
           role: str = "user", metadata: dict | None = None, by: str | None = None, value: str | None = None) -> tuple[str, dict]:
    """A new key of the tenant; returns (value, info). The value is random unless given (Terraform's bootstrap)."""
    value = value or new_value()
    with db.pool().connection() as c:
        try:
            with c.transaction():
                r = c.execute(f"""insert into keys (hash, alias, tenant, role, models, budget, expires, preview, metadata, created_by)
                                  values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning {COLS}""",
                              (key_hash(value), alias, tenant, role, json.dumps(list(models or [])), budget, _expiry(expires_days),
                               preview(value), json.dumps(metadata or {}), by)).fetchone()
        except Exception as e:  # noqa: BLE001 - a duplicate alias is the caller's 409, anything else propagates
            _conflict(e, alias)
    i = _info(r)
    mirror_create(value, i)
    return value, i


def update(tenant: str, alias: str, budget: float | None = None, models: list[str] | None = None, expires_days: int | None = None) -> dict | None:
    """Budget, allowed models, expiry (days from now; 0 = never); fields left None stay. Spend never changes here."""
    sets, args = [], []
    if budget is not None:
        sets.append("budget = %s"); args.append(budget)
    if models is not None:
        sets.append("models = %s"); args.append(json.dumps(models))
    if expires_days is not None:
        sets.append("expires = %s"); args.append(_expiry(expires_days))
    with db.pool().connection() as c, c.transaction():
        r = c.execute(f"update keys set {', '.join(sets + ['updated_at = now()'])} where tenant = %s and alias = %s returning {COLS}",
                      (*args, tenant, alias)).fetchone()
    if r is None:
        return None
    i = _info(r)
    mirror_update(i)
    return i


def block(tenant: str, alias: str, blocked: bool = True) -> dict | None:
    with db.pool().connection() as c, c.transaction():
        r = c.execute(f"update keys set blocked = %s, updated_at = now() where tenant = %s and alias = %s returning {COLS}", (blocked, tenant, alias)).fetchone()
    if r is None:
        return None
    i = _info(r)
    mirror_update(i)
    return i


def revoke(tenant: str, alias: str) -> bool:
    with db.pool().connection() as c, c.transaction():
        r = c.execute("delete from keys where tenant = %s and alias = %s returning hash", (tenant, alias)).fetchone()
    if r is None:
        return False
    mirror_delete(r[0])
    return True


def add_spend(h: str, usd: float, operation: str, gpu_s: float) -> float | None:
    """Charge one finished run to a key: the receipt (`charges`, unique per operation) and the key's spend commit
    together, so a retry after a failed Job annotation never charges twice and concurrent charges never lose an
    increment. A charge of a key that was revoked meanwhile stays in the ledger (its hash names the key) and the
    run counts as billed; returns the key's spend, None when the key is gone."""
    with db.pool().connection() as c, c.transaction():
        inserted = c.execute("""insert into charges (operation, key_hash, gpu_seconds, usd) values (%s, %s, %s, %s)
                                on conflict do nothing returning operation""", (operation, h, gpu_s, usd)).fetchone()
        if inserted:
            row = c.execute("update keys set spend = spend + %s, updated_at = now() where hash = %s returning spend", (usd, h)).fetchone()
            if row is None:
                log.warning("charge %s: key %s... no longer exists; recorded in the ledger only", operation, h[:8])
                return None
            return float(row[0])
        receipt = c.execute("select key_hash from charges where operation = %s", (operation,)).fetchone()
        if receipt[0] != h:
            raise ValueError("operation already belongs to another billing key")
        row = c.execute("select spend from keys where hash = %s", (h,)).fetchone()
        return float(row[0]) if row else None


def ensure_router(value: str) -> None:
    """The per-fleet router key (ROUTER_KEY): upserted at every API start, so a rotated Secret takes effect with
    a restart and the previous value stops working."""
    h = key_hash(value)
    with db.pool().connection() as c, c.transaction():
        c.execute("delete from keys where tenant = %s and alias = %s and hash <> %s", (ROUTER["tenant"], ROUTER["alias"], h))
        c.execute("""insert into keys (hash, alias, tenant, role, preview, created_by) values (%s, %s, %s, %s, %s, 'platform')
                     on conflict (hash) do update set role = excluded.role, blocked = false, budget = null, expires = null, models = '[]', updated_at = now()""",
                  (h, ROUTER["alias"], ROUTER["tenant"], ROUTER["role"], preview(value)))


def bootstrap(value: str, alias: str, tenant: str, role: str = "user", budget: float | None = None, models: list[str] | None = None,
              routes: list[str] | None = None) -> dict:
    """A Terraform-defined key: exists with this value and these settings when this returns. Spend and creation
    time of an existing key stay; a key of the same alias with another value (a `-replace`d random_password)
    is replaced in place, its spend carried over. Idempotent; mirrored into LiteLLM."""
    h, meta = key_hash(value), json.dumps({"allowed_passthrough_routes": list(routes or [])})
    with db.pool().connection() as c, c.transaction():
        r = c.execute(f"""update keys set hash = %s, preview = %s, role = %s, budget = %s, models = %s, metadata = %s, blocked = false, updated_at = now()
                          where tenant = %s and alias = %s returning {COLS}""",
                      (h, preview(value), role, budget, json.dumps(list(models or [])), meta, tenant, alias)).fetchone()
        if r is None:
            r = c.execute(f"""insert into keys (hash, alias, tenant, role, models, budget, preview, metadata, created_by) values (%s, %s, %s, %s, %s, %s, %s, %s, 'terraform')
                              returning {COLS}""", (h, alias, tenant, role, json.dumps(list(models or [])), budget, preview(value), meta)).fetchone()
    i = _info(r)
    mirror_create(value, i)
    return i


def import_litellm() -> int:
    """Once at API start: every key LiteLLM holds that the table does not, with its hash, alias, tenant, role, budget,
    spend, expiry, block flag and pass-through routes, so an upgrade changes no key for anyone. Keys without a
    tenant and LiteLLM-internal keys (role `internal`, the former platform key of the model groups) are skipped.
    Never overwrites a row: the table is the source of truth. Returns the number of keys imported."""
    if not LITELLM_MASTER_KEY:
        return 0
    import httpx
    found, page = [], 1
    with httpx.Client(timeout=20) as c:
        while True:
            r = c.get(f"{LITELLM_URL}/key/list", params={"return_full_object": "true", "size": 100, "page": page},
                      headers={"Authorization": f"Bearer {LITELLM_MASTER_KEY}"})
            if r.status_code >= 300:
                raise RuntimeError(f"litellm /key/list: {r.status_code} {r.text[:160]}")
            data = r.json()
            items = [k for k in data.get("keys", []) if isinstance(k, dict)]
            found += items
            if len(items) < 100 or page >= int(data.get("total_pages") or 1):
                break
            page += 1
    n = 0
    with db.pool().connection() as c, c.transaction():
        for k in found:
            meta = k.get("metadata") or {}
            if not k.get("token") or not meta.get("tenant") or meta.get("role") == "internal":
                continue
            extra = {x: v for x, v in meta.items() if x not in ("tenant", "role")}
            row = c.execute("""insert into keys (hash, alias, tenant, role, models, budget, spend, expires, blocked, preview, metadata, created_at, created_by)
                               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, coalesce(%s, now()), 'litellm-import')
                               on conflict do nothing returning hash""",
                            (k["token"], k.get("key_alias") or k["token"][:12], meta["tenant"], meta.get("role") or "user", json.dumps(k.get("models") or []),
                             k.get("max_budget"), k.get("spend") or 0, _when(k.get("expires")), bool(k.get("blocked")), k.get("key_name"),
                             json.dumps(extra), _when(k.get("created_at")))).fetchone()
            n += 1 if row else 0
    return n


# ---- the LiteLLM mirror (best effort; the fleet database is the source of truth) ----------------------
def _litellm(method: str, path: str, body: dict) -> int:
    """Status code of a LiteLLM admin call, 0 when LiteLLM is unreachable; never raises."""
    if not LITELLM_MASTER_KEY:
        return 0
    import httpx
    try:
        with httpx.Client(timeout=10) as c:
            r = c.request(method, LITELLM_URL + path, json=body, headers={"Authorization": f"Bearer {LITELLM_MASTER_KEY}"})
    except Exception as e:  # noqa: BLE001
        log.warning("litellm mirror %s: %s", path, e)
        return 0
    if r.status_code >= 300:
        log.warning("litellm mirror %s: %s %s", path, r.status_code, r.text[:160])
    return r.status_code


def _mirror_body(i: dict) -> dict:
    body = {"max_budget": i["max_budget"], "models": i["models"], "metadata": i["metadata"], "blocked": i["blocked"]}
    if i.get("expires"):
        left = (datetime.fromisoformat(i["expires"]) - datetime.now(timezone.utc)).total_seconds()
        body["duration"] = f"{max(int(left), 1)}s"
    return body


def mirror_create(value: str, i: dict) -> None:
    """LiteLLM accepts a provided key value: the same key works on its OpenAI route."""
    code = _litellm("POST", "/key/generate", {"key": value, "key_alias": i["key_alias"], **_mirror_body(i)})
    if code == 400:          # exists already (a re-run of the bootstrap, a restored database): bring it in line
        mirror_update(i)


def mirror_update(i: dict) -> None:
    body = _mirror_body(i)
    if "duration" not in body:
        body["duration"] = None
    _litellm("POST", "/key/update", {"key": i["token"], **body})


def mirror_delete(h: str) -> None:
    _litellm("POST", "/key/delete", {"keys": [h]})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stdout)
    if sys.argv[1:2] != ["bootstrap"]:
        sys.exit("usage: python -m keys bootstrap   (env KEY, KEY_SPEC)")
    spec, value = json.loads(os.environ["KEY_SPEC"]), os.environ["KEY"]
    for attempt in range(60):            # the managed database may still be coming up
        try:
            db.migrate()
            break
        except Exception as e:  # noqa: BLE001
            log.warning("database not ready (%s), attempt %d", e, attempt + 1)
            time.sleep(10)
    else:
        sys.exit("database unavailable")
    i = bootstrap(value, spec["alias"], spec["tenant"], spec.get("role") or "user", spec.get("budget"), spec.get("models"), spec.get("routes"))
    log.info("key %s of tenant %s ready (role %s, budget %s)", i["key_alias"], spec["tenant"], i["metadata"]["role"], i["max_budget"])
