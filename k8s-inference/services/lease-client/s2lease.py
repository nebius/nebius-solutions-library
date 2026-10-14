"""s2lease: the thin worker client of Serverless 2.0 native execution leases (docs/API.md "Native execution leases").

Standard library only; copy this file into a model image or vendor it. The platform projects everything the client
needs into an opted-in endpoint pod (`native_lease: optional | required` on the model): the token file
(`S2_LEASE_TOKEN_FILE`, a ServiceAccount token bound to the pod, renewed by the kubelet), the control API
(`S2_API_URL`), the mode (`S2_LEASE_MODE`) and, on a fleet whose control hostnames are not publicly signed, the
CA bundle to trust (`S2_CA_PEM`, public certificate material). The request's markers come from the gateway, verified by the hook and
forwarded only from that context: headers `x-serverless2-operation` and `x-serverless2-attempt` (empty on a direct
call, which carries no lease).

    from s2lease import Lease, LeaseRefused

    lease = Lease.from_env()                      # None when the pod has no lease projection
    exe = lease.attach(headers, worker_request_id=my_id)   # BEFORE any GPU enqueue; raises LeaseRefused -> fail closed
    ...                                           # run; poll exe.cancel_requested() between steps if cancellable
    exe.release("done", gpu_seconds=None)         # AFTER your own settlement; None = unknown, never 0 unless measured

Semantics the client guarantees: attach is retried on transport errors AND on 5xx answers with the same tuple (idempotent
on the API: a recorded attach whose answer was lost comes back 200 on the retry), never with a new request id; a 4xx
answer is definite and final for this request (LeaseRefused, with `status_code` and `detail`: 401/403 the identity, 409
the caller is over, cancelled, stale, or another worker holds it) and says nothing about the worker, so it must not
quarantine the worker; retries exhausted (transport or 5xx) is LeaseUnavailable, with `status_code` (the last 5xx, or
None): ambiguous for an attach, hold rather than run; release is retried the same way and is idempotent; a request
without markers attaches nothing (optional mode) or raises LeaseRefused (required mode: the worker must not run an
unleased request)."""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request

OPERATION_HEADER = "x-serverless2-operation"
ATTEMPT_HEADER = "x-serverless2-attempt"


class LeaseRefused(Exception):
    """The platform definitely refused this request (a 4xx: 401/403 the identity, 409 the lease state): fail closed, do
    not run the request. `status_code` and `detail` are the API's answer. A refusal is about the request, never about
    the worker: it must not take the worker out of service."""

    def __init__(self, status_code: int | None, detail: str = "", action: str = ""):
        self.status_code, self.detail, self.action = status_code, detail, action
        super().__init__(f"{action or 'lease'} refused ({status_code}): {detail}")


class LeaseUnavailable(Exception):
    """No definite answer after the retries (transport errors or 5xx): fail closed as well. For an attach this is
    ambiguous (the API may or may not have recorded it): hold, do not run; a later retry with the same tuple is safe.
    `status_code` is the last 5xx seen, or None when nothing answered."""

    def __init__(self, status_code: int | None, detail: str = "", action: str = ""):
        self.status_code, self.detail, self.action = status_code, detail, action
        super().__init__(f"{action or 'lease'} unavailable ({status_code or 'no answer'}): {detail}")


_SSL: ssl.SSLContext | None = None


def _ssl_context() -> ssl.SSLContext:
    """The system roots, plus the fleet's CA bundle when the platform projected one (`S2_CA_PEM`)."""
    global _SSL
    if _SSL is None:
        ctx = ssl.create_default_context()  # the system roots (S2_API_URL is publicly signed on a public fleet)
        pem = os.environ.get("S2_CA_PEM") or ""
        if "BEGIN CERTIFICATE" in pem:
            ctx.load_verify_locations(cadata=pem)  # ADDED to the roots, never replacing them
        _SSL = ctx
    return _SSL


def _default_transport(method: str, url: str, body: dict | None, headers: dict, timeout: float) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:  # noqa: S310 - own API
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except ValueError:
            payload = {}
        return e.code, payload


class Execution:
    """One attached native execution: the operation, the attempt, this worker's request id, the owner context."""

    def __init__(self, lease: "Lease", operation: str, attempt: str, worker_request_id: str, state: dict):
        self.lease, self.operation, self.attempt, self.worker_request_id = lease, operation, attempt, worker_request_id
        self.state = state
        self.owner = state.get("owner")  # {tenant, owner, model}: the trusted owner group of the admitted call

    def cancel_requested(self) -> bool:
        try:  # one attempt, no backoff: a poll between steps must not stall the worker; the next step polls again
            code, body = self.lease._call("GET", f"/internal/executions/{self.operation}", action="poll", retries=1)
        except LeaseUnavailable:
            return False
        if code == 200:
            self.state = body
            return bool(body.get("cancel_requested"))
        return False  # unknown: keep going; the platform never releases on a failed read either

    def release(self, outcome: str, gpu_seconds: float | None = None) -> dict:
        """After the worker's own settlement (natural completion or the stop ACK of a drain). `gpu_seconds` only as
        measured; None = unknown. Idempotent: retried with the same tuple on transport errors."""
        code, body = self.lease._call(
            "POST",
            f"/internal/executions/{self.operation}:release",
            {
                "attempt": self.attempt,
                "worker_request_id": self.worker_request_id,
                "outcome": outcome,
                "gpu_seconds": gpu_seconds,
            },
            action="release",
        )
        if code == 200:
            self.state = body
            return body
        raise LeaseRefused(code, str(body.get("detail") or ""), "release")


class Lease:
    def __init__(self, api_url: str, token_file: str, mode: str = "optional", transport=None, timeout: float = 10.0):
        self.api_url, self.token_file, self.mode = api_url.rstrip("/"), token_file, mode
        self.transport, self.timeout = transport or _default_transport, timeout
        self.retries, self.backoff = 5, 0.5

    @classmethod
    def from_env(cls, transport=None) -> "Lease | None":
        url, token = os.environ.get("S2_API_URL"), os.environ.get("S2_LEASE_TOKEN_FILE")
        if not url or not token:
            return None
        return cls(url, token, os.environ.get("S2_LEASE_MODE", "optional"), transport=transport)

    def _token(self) -> str:
        with open(self.token_file) as f:
            return f.read().strip()

    def _call(
        self, method: str, path: str, body: dict | None = None, action: str = "", retries: int | None = None
    ) -> tuple[int, dict]:
        """One request, retried with the same body on transport errors and 5xx answers (ambiguous: the API may have
        acted; every lease route is idempotent per tuple). A 2xx or 4xx is definite and returned at once."""
        last_code: int | None = None
        last_detail = ""
        retries = self.retries if retries is None else retries
        for i in range(retries):
            try:
                code, payload = self.transport(
                    method, self.api_url + path, body, {"Authorization": f"Bearer {self._token()}"}, self.timeout
                )
            except (OSError, urllib.error.URLError, ValueError) as e:  # transport: no answer
                last_code, last_detail = None, f"{type(e).__name__}: {e}"
            else:
                if code < 500:
                    return code, payload
                last_code, last_detail = code, str((payload or {}).get("detail") or "")
            if i + 1 < retries:
                time.sleep(self.backoff * (2**i))
        raise LeaseUnavailable(last_code, f"{method} {path}: {last_detail}", action)

    @staticmethod
    def markers(headers) -> tuple[str, str]:
        """The verified markers of a request (case-insensitive header lookup); ("", "") on a direct call."""
        get = headers.get if hasattr(headers, "get") else dict(headers).get
        return (get(OPERATION_HEADER) or get(OPERATION_HEADER.title()) or "").strip(), (
            get(ATTEMPT_HEADER) or get(ATTEMPT_HEADER.title()) or ""
        ).strip()

    def attach(self, headers, worker_request_id: str) -> Execution | None:
        """Before any GPU enqueue for the request. Returns None for an unleased request in optional mode; raises
        LeaseRefused in required mode or when the platform refuses (the worker must not run the request)."""
        operation, attempt = self.markers(headers)
        if not operation or not attempt:
            if self.mode == "required":
                raise LeaseRefused(
                    None, "no lease markers on the request (direct call); required mode refuses it", "attach"
                )
            return None
        code, body = self._call(
            "POST",
            f"/internal/executions/{operation}:attach",
            {"attempt": attempt, "worker_request_id": worker_request_id},
            action="attach",
        )
        if code == 200:
            return Execution(self, operation, attempt, worker_request_id, body)
        raise LeaseRefused(code, str(body.get("detail") or ""), "attach")
