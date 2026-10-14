"""Bearer key -> Principal: the fleet database answers (hash lookup, cached AUTH_CACHE_S); nothing else authenticates a
caller. LiteLLM holds a best-effort mirror of the keys for its own OpenAI route (keys.py) and is never consulted here."""

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

import config as cfg
import db
import keys
from config import AUTH_CACHE_S, TENANT_NS_PREFIX

_cache: dict[str, tuple[float, dict]] = {}


@dataclass
class Principal:
    key: str
    tenant: str
    info: dict
    key_hash: str | None = None  # an operation-scoped principal (an admitted child's caller): the key's hash, no key

    @property
    def hash(self) -> str:
        return self.key_hash or keys.key_hash(self.key)

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
        identity = (
            [self.tenant, "principal", principal_id] if principal_id else [self.tenant, "key", keys.key_hash(self.key)]
        )
        import json

        return keys.key_hash(json.dumps(identity, separators=(",", ":")))

    def public(self) -> dict:
        i = self.info
        legacy = (i.get("metadata") or {}).get("legacy") or {}
        return {
            "id": i.get("token") or i.get("key_name"),
            "key_preview": i.get("key_name"),
            "alias": i.get("key_alias"),
            "tenant": self.tenant,
            "budget": i.get("max_budget"),
            "spend": i.get("spend", 0),
            "models": i.get("models", []),
            "created_at": i.get("created_at"),
            "expires_at": i.get("expires"),
            "status": "exhausted" if exhausted(i) else "active",
            "role": self.role,
            **limits_view(i),
            "legacy": legacy or None,
        }


def exhausted(i: dict) -> bool:
    """Whether a key record has used up any of its budgets (money, requests, GPU seconds)."""
    return (
        (i.get("max_budget") is not None and (i.get("spend") or 0) >= i["max_budget"])
        or (i.get("budget_requests") is not None and (i.get("spent_requests") or 0) >= i["budget_requests"])
        or (i.get("budget_gpu_seconds") is not None and (i.get("spent_gpu_seconds") or 0) >= i["budget_gpu_seconds"])
    )


def limits_view(i: dict) -> dict:
    """The safe limit and usage fields of a key record (docs/API.md "Keys"): scopes, concurrency, the request and
    GPU-second budgets with their spend, the rate limit and the stable principal id. Never the hash, the raw metadata
    or a secret. Shared by the caller's own view (`GET /v1/keys/me`) and the admin listing (`GET /v1/keys`)."""
    legacy = (i.get("metadata") or {}).get("legacy") or {}
    return {
        "scopes": i.get("scopes") or [],
        "max_concurrent": i.get("max_concurrent"),
        "budget_requests": i.get("budget_requests"),
        "spent_requests": i.get("spent_requests") or 0,
        "budget_gpu_seconds": i.get("budget_gpu_seconds"),
        "spent_gpu_seconds": i.get("spent_gpu_seconds") or 0,
        "rate_limit": i.get("rate_limit"),
        "principal_id": legacy.get("principal_id"),
    }


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


bearer = HTTPBearer(auto_error=False, scheme_name="bearer")


async def principal(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
    return await _principal(f"{credentials.scheme} {credentials.credentials}" if credentials else "")


def check_budget(p: Principal) -> None:
    """The cached view (a fast refusal); `keys.admit` holds the exact, transactional check at admission."""
    i = p.info
    if i.get("max_budget") is not None and (i.get("spend") or 0) >= i["max_budget"]:
        raise HTTPException(402, "key budget exhausted")
    if i.get("budget_requests") is not None and (i.get("spent_requests") or 0) >= i["budget_requests"]:
        raise HTTPException(402, "key request budget exhausted")
    if i.get("budget_gpu_seconds") is not None and (i.get("spent_gpu_seconds") or 0) >= i["budget_gpu_seconds"]:
        raise HTTPException(402, "key GPU-second budget exhausted")


def check_scope(p: Principal, scope: str | tuple[str, ...]) -> None:
    """A key with scopes may do only what they name (keys.SCOPES); a key without scopes may do everything of its role.
    A tuple: any one of them suffices."""
    scopes = p.info.get("scopes") or []
    wanted = scope if isinstance(scope, tuple) else (scope,)
    if scopes and not any(w in scopes for w in wanted):
        raise HTTPException(403, f"key scope {' or '.join(wanted)} required")


def check_model(p: Principal, model: str) -> None:
    if not allows_model(p, model):
        raise HTTPException(403, f"key is not allowed to use model {model}")


def allows_model(p: Principal, model: str) -> bool:
    allowed = p.info.get("models") or []
    return not allowed or model in allowed or "all-proxy-models" in allowed


def scoped(*scopes: str):
    """A route dependency: authenticate once and require every named permission before calling the handler."""

    def dependency(p: Principal = Depends(principal)) -> Principal:
        for scope in scopes:
            check_scope(p, scope)
        return p

    return dependency


def authorize(
    p: Principal, model: str, scope: str | tuple[str, ...], *, budget: bool = False, model_check: bool = True
) -> None:
    """Shared model policy for API invokes and the model gateway's authorization hook. `model_check=False` only for a
    delegated stage (app._require_delegation verified the delegation first)."""
    if model_check:
        check_model(p, model)
    if budget:
        check_budget(p)
    check_scope(p, scope)


def check_operation_owner(p: Principal, job: dict) -> None:
    md = job["metadata"]
    labels, ann = md.get("labels") or {}, md.get("annotations") or {}
    missing = HTTPException(404, "operation not found")
    if labels.get(f"{cfg.LABEL}/tenant") != p.tenant:
        raise missing
    model = labels.get(f"{cfg.LABEL}/model")
    root = ann.get(f"{cfg.LABEL}/resumed-from") or md["name"]
    record = keys.operation_owner(root) if db.enabled() else None
    if record:
        tenant, owner, model = record
        if tenant != p.tenant or (p.role != "admin" and owner != p.owner):
            raise missing
    elif ann.get(f"{cfg.LABEL}/owner"):
        if p.role != "admin" and ann[f"{cfg.LABEL}/owner"] != p.owner:
            raise missing
    elif p.role != "admin":
        # Pre-migration rows have no durable provenance. A matching original
        # key is evidence; sharing its tenant/bucket is not.
        if ann.get(f"{cfg.LABEL}/key") != keys.key_hash(p.key):
            raise missing
    if model and not allows_model(p, model) and not _delegated_stage(labels, ann, model):
        raise missing


def _delegated_stage(labels: dict, ann: dict, model: str) -> bool:
    """An owner's stage on a model the key is not granted (docs/JOBS.md "Delegated stages") stays readable by the owner:
    the Job is a stage of a graph that declared it on this model and whose root class delegates to it."""
    root = labels.get(f"{cfg.LABEL}/stage-root")
    stage = ann.get(f"{cfg.LABEL}/stage")
    if not root or not stage or not db.enabled():
        return False
    g = keys.graph_of(root)
    if not g or (g.get("models") or {}).get(stage) != model:
        return False
    record = keys.operation_owner(root)
    if not record:
        return False
    import catalog

    root_model = catalog.get(record[2])
    return bool(root_model and model in (root_model.get("delegates") or []))


def may_read_operation(p: Principal, job: dict) -> bool:
    try:
        check_operation_owner(p, job)
        return True
    except HTTPException as e:
        if e.status_code != 404:
            raise
        return False


def admin(p: Principal = Depends(principal)) -> Principal:
    if (p.info.get("metadata") or {}).get("role") != "admin":
        raise HTTPException(403, "endpoint scaling and key management need a key with metadata.role=admin")
    return p
