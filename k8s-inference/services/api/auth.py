"""Bearer key -> tenant, via LiteLLM /key/info with the master key (cached)."""
import hashlib, time
from dataclasses import dataclass
import httpx
from fastapi import Header, HTTPException
from config import AUTH_CACHE_S, LITELLM_MASTER_KEY, LITELLM_URL, TENANT_NS_PREFIX

_cache: dict[str, tuple[float, dict]] = {}


@dataclass
class Principal:
    key: str
    tenant: str
    info: dict

    @property
    def namespace(self) -> str:
        return f"{TENANT_NS_PREFIX}{self.tenant}"

    def public(self) -> dict:
        i = self.info
        exhausted = i.get("max_budget") is not None and (i.get("spend") or 0) >= i["max_budget"]
        return {"id": i.get("token") or i.get("key_name"), "key_preview": i.get("key_name"), "alias": i.get("key_alias"),
                "tenant": self.tenant, "budget": i.get("max_budget"), "spend": i.get("spend", 0), "models": i.get("models", []),
                "created_at": i.get("created_at"), "expires_at": i.get("expires"),
                "status": "exhausted" if exhausted else "active", "role": (i.get("metadata") or {}).get("role", "user")}


async def key_info(key: str) -> dict:
    h = hashlib.sha256(key.encode()).hexdigest()
    now = time.time()
    hit = _cache.get(h)
    if hit and hit[0] > now:
        return hit[1]
    from resilience import retry_http
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await retry_http(lambda: c.get(f"{LITELLM_URL}/key/info", params={"key": key}, headers={"Authorization": f"Bearer {LITELLM_MASTER_KEY}"}))
    except httpx.HTTPError as e:
        raise HTTPException(503, f"key service unavailable: {type(e).__name__}")
    if r.status_code >= 500:
        raise HTTPException(503, "key service unavailable")
    if r.status_code != 200:
        raise HTTPException(401, "invalid API key")
    info = r.json().get("info") or {}
    _cache[h] = (now + AUTH_CACHE_S, info)
    return info


def forget(key: str) -> None:
    _cache.pop(hashlib.sha256(key.encode()).hexdigest(), None)


async def principal(authorization: str = Header(default="")) -> Principal:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer key")
    key = authorization[7:].strip()
    info = await key_info(key)
    if info.get("blocked"):
        raise HTTPException(403, "key is blocked")
    if info.get("expires") and info["expires"] < time.strftime("%Y-%m-%dT%H:%M:%S"):
        raise HTTPException(403, "key expired")
    tenant = (info.get("metadata") or {}).get("tenant")
    if not tenant:
        raise HTTPException(403, "key is not bound to a tenant")
    return Principal(key=key, tenant=tenant, info=info)


def check_budget(p: Principal) -> None:
    i = p.info
    if i.get("max_budget") is not None and (i.get("spend") or 0) >= i["max_budget"]:
        raise HTTPException(402, "key budget exhausted")


def check_model(p: Principal, model: str) -> None:
    allowed = p.info.get("models") or []
    if allowed and model not in allowed and "all-proxy-models" not in allowed:
        raise HTTPException(403, f"key is not allowed to use model {model}")
