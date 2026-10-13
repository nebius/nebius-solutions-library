"""Bearer key -> Principal: the fleet database answers (hash lookup, cached AUTH_CACHE_S); nothing else authenticates a
caller. LiteLLM holds a best-effort mirror of the keys for its own OpenAI route (keys.py) and is never consulted here."""
import asyncio, time
from dataclasses import dataclass
from datetime import datetime, timezone
import httpx
from fastapi import Header, HTTPException
import db, keys
from config import AUTH_CACHE_S, TENANT_NS_PREFIX

_cache: dict[str, tuple[float, dict]] = {}


@dataclass
class Principal:
    key: str
    tenant: str
    info: dict

    @property
    def namespace(self) -> str:
        return f"{TENANT_NS_PREFIX}{self.tenant}"

    @property
    def role(self) -> str:
        return (self.info.get("metadata") or {}).get("role") or "user"

    @property
    def owner(self) -> str:
        """Opaque stable operation owner; multiple imported keys of one principal agree."""
        principal_id = ((self.info.get("metadata") or {}).get("legacy") or {}).get("principal_id")
        identity = [self.tenant, "principal", principal_id] if principal_id else [self.tenant, "key", keys.key_hash(self.key)]
        import json
        return keys.key_hash(json.dumps(identity, separators=(",", ":")))

    def public(self) -> dict:
        i = self.info
        exhausted = (i.get("max_budget") is not None and (i.get("spend") or 0) >= i["max_budget"]) \
            or (i.get("budget_requests") is not None and (i.get("spent_requests") or 0) >= i["budget_requests"]) \
            or (i.get("budget_gpu_seconds") is not None and (i.get("spent_gpu_seconds") or 0) >= i["budget_gpu_seconds"])
        legacy = (i.get("metadata") or {}).get("legacy") or {}
        return {"id": i.get("token") or i.get("key_name"), "key_preview": i.get("key_name"), "alias": i.get("key_alias"),
                "tenant": self.tenant, "budget": i.get("max_budget"), "spend": i.get("spend", 0), "models": i.get("models", []),
                "created_at": i.get("created_at"), "expires_at": i.get("expires"),
                "status": "exhausted" if exhausted else "active", "role": self.role,
                "scopes": i.get("scopes") or [], "max_concurrent": i.get("max_concurrent"),
                "budget_requests": i.get("budget_requests"), "spent_requests": i.get("spent_requests") or 0,
                "budget_gpu_seconds": i.get("budget_gpu_seconds"), "spent_gpu_seconds": i.get("spent_gpu_seconds") or 0,
                "rate_limit": i.get("rate_limit"), "principal_id": legacy.get("principal_id"), "legacy": legacy or None}


async def _fetch(url: str, headers: dict, params: dict | None = None) -> dict | None:
    """GET a key service: its JSON, None for a key it does not know, 503 when it is down (fail closed)."""
    from resilience import retry_http
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await retry_http(lambda: c.get(url, params=params, headers=headers))
    except httpx.HTTPError as e:
        raise HTTPException(503, f"key service unavailable: {type(e).__name__}")
    if r.status_code >= 500:
        raise HTTPException(503, "key service unavailable")
    if r.status_code != 200:
        return None
    return r.json()


async def key_info(key: str) -> dict:
    h = keys.key_hash(key)
    now = time.time()
    hit = _cache.get(h)
    if hit and hit[0] > now:
        return hit[1]
    info = await asyncio.to_thread(keys.info, h) if db.enabled() else None
    if info is None:
        raise HTTPException(401, "invalid API key")
    _cache[h] = (now + AUTH_CACHE_S, info)
    return info


def forget(key: str) -> None:
    _cache.pop(keys.key_hash(key), None)


def forget_hash(h: str) -> None:
    _cache.pop(h, None)


async def _principal(authorization: str, allow_router: bool = False) -> Principal:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer key")
    key = authorization[7:].strip()
    info = await key_info(key)
    if info.get("blocked"):
        raise HTTPException(403, "key is blocked")
    if info.get("expires"):
        expiry = datetime.fromisoformat(info["expires"].replace("Z", "+00:00"))
        if (expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc)) <= datetime.now(timezone.utc):
            raise HTTPException(403, "key expired")
    meta = info.get("metadata") or {}
    if not meta.get("tenant"):
        raise HTTPException(403, "key is not bound to a tenant")
    if meta.get("role") == keys.ROUTER["role"] and not allow_router:
        # LiteLLM's deployments carry it to the endpoints' gateway hostnames; it defines nothing, runs nothing, sees no tenant
        raise HTTPException(403, "the router key is accepted at the model gateway only")
    return Principal(key=key, tenant=meta["tenant"], info=info)


async def principal(authorization: str = Header(default="")) -> Principal:
    return await _principal(authorization)


def check_budget(p: Principal) -> None:
    """The cached view (a fast refusal); `keys.admit` holds the exact, transactional check at admission."""
    i = p.info
    if i.get("max_budget") is not None and (i.get("spend") or 0) >= i["max_budget"]:
        raise HTTPException(402, "key budget exhausted")
    if i.get("budget_requests") is not None and (i.get("spent_requests") or 0) >= i["budget_requests"]:
        raise HTTPException(402, "key request budget exhausted")
    if i.get("budget_gpu_seconds") is not None and (i.get("spent_gpu_seconds") or 0) >= i["budget_gpu_seconds"]:
        raise HTTPException(402, "key GPU-second budget exhausted")


def check_scope(p: Principal, scope: str) -> None:
    """A key with scopes may do only what they name (keys.SCOPES); a key without scopes may do everything of its role."""
    scopes = p.info.get("scopes") or []
    if scopes and scope not in scopes:
        raise HTTPException(403, f"key scope {scope} required")


def check_model(p: Principal, model: str) -> None:
    if not allows_model(p, model):
        raise HTTPException(403, f"key is not allowed to use model {model}")


def allows_model(p: Principal, model: str) -> bool:
    allowed = p.info.get("models") or []
    return not allowed or model in allowed or "all-proxy-models" in allowed
