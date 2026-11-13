"""A proxied synchronous call ends when its caller goes away (api 0.11.127, docs/API.md "Synchronous calls"): the
downstream request is cancelled, the client closed, 499 raised so the caller's `finally` releases the admission; a
timeout still answers 504; a normal answer is returned untouched."""

import asyncio
import time

import httpx
import pytest
from fastapi import HTTPException

import app as appmod
from test_api import H, client, cluster  # noqa: F401 - pytest fixture registration


class _Request:
    """A request whose server reports the caller gone after `disconnect_after_s` (never: a long time)."""

    def __init__(self, disconnect_after_s: float):
        self.disconnect_after_s = disconnect_after_s

    async def receive(self):
        await asyncio.sleep(self.disconnect_after_s)
        return {"type": "http.disconnect"}


def _client(monkeypatch, post, closed):
    class Client:
        def __init__(self, timeout=None):
            self.timeout = timeout

        async def post(self, url, json=None, headers=None):
            return await post()

        async def aclose(self):
            closed.append(True)

    monkeypatch.setattr(appmod.httpx, "AsyncClient", Client)


def test_a_disconnected_caller_cancels_the_downstream_call(monkeypatch):
    closed, cancelled = [], []
    monkeypatch.setattr(appmod, "DISCONNECT_POLL_S", 0.01)

    async def slow():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return httpx.Response(200, json={"ok": True})

    _client(monkeypatch, slow, closed)
    req = _Request(0.05)

    async def run():
        with pytest.raises(HTTPException) as e:
            await appmod._proxy_until_answer_or_disconnect("http://x", {}, {}, 5, req)
        return e.value

    e = asyncio.run(run())
    assert e.status_code == 499 and "disconnected" in e.detail
    assert closed == [True] and cancelled == [True]


def test_an_answer_timeout_or_error_behave_as_before(monkeypatch):
    closed = []
    monkeypatch.setattr(appmod, "DISCONNECT_POLL_S", 0.01)

    async def ok():
        return httpx.Response(200, json={"ok": True})

    _client(monkeypatch, ok, closed)
    r = asyncio.run(appmod._proxy_until_answer_or_disconnect("http://x", {}, {}, 5, _Request(99)))
    assert r.status_code == 200 and closed == [True]

    async def slow():
        raise httpx.ReadTimeout("slow")

    _client(monkeypatch, slow, closed)
    with pytest.raises(HTTPException) as e:
        asyncio.run(appmod._proxy_until_answer_or_disconnect("http://x", {}, {}, 7, _Request(99)))
    assert e.value.status_code == 504 and "7s" in e.value.detail

    async def down():
        raise httpx.ConnectError("nope")

    _client(monkeypatch, down, closed)
    with pytest.raises(HTTPException) as e:
        asyncio.run(appmod._proxy_until_answer_or_disconnect("http://x", {}, {}, 7, None))
    assert e.value.status_code == 502

    # a caller that never disconnects while the endpoint answers slowly: the answer, not a 499
    async def late():
        await asyncio.sleep(0.05)
        return httpx.Response(201, json={})

    _client(monkeypatch, late, closed)
    assert asyncio.run(appmod._proxy_until_answer_or_disconnect("http://x", {}, {}, 5, _Request(99))).status_code == 201


def test_a_disconnect_reaches_the_proxy_through_the_real_middleware(client, monkeypatch):
    """The ASGI app itself, with its `no_store` BaseHTTPMiddleware (Starlette's wrapped receive): a caller that sends
    its body and then disconnects while the endpoint is still working ends the downstream call within a second and
    releases the admission. `Request.is_disconnected()` never sees this (starlette discussion 2094); the awaited
    receive watcher does."""
    import json

    c, fake = client
    cancelled, released = [], []

    class Client:
        def __init__(self, timeout=None):
            pass

        async def post(self, url, json=None, headers=None):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            return httpx.Response(200, json={"ok": True})

        async def aclose(self):
            return None

    monkeypatch.setattr(appmod.httpx, "AsyncClient", Client)
    monkeypatch.setattr(appmod, "_release", lambda sid, gpu: released.append(sid))
    body = json.dumps({"input": {"messages": []}}).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/models/llm-example:invoke",
        "raw_path": b"/v1/models/llm-example:invoke",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"authorization", b"Bearer sk-good"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }

    async def run():
        queue = [{"type": "http.request", "body": body, "more_body": False}]
        gone = asyncio.Event()

        async def receive():
            if queue:
                return queue.pop(0)
            await gone.wait()
            return {"type": "http.disconnect"}

        sent = []

        async def send(message):
            sent.append(message)

        task = asyncio.create_task(appmod.app(scope, receive, send))
        await asyncio.sleep(0.3)  # the downstream call is in flight, the body consumed
        assert not cancelled and not released
        t0 = time.time()
        gone.set()
        await asyncio.wait_for(task, 5)
        return time.time() - t0, sent

    took, sent = asyncio.run(run())
    assert cancelled == [True], "the downstream request was not cancelled on the caller's disconnect"
    assert len(released) == 1 and released[0].startswith("sync-")
    assert took < 2, f"the disconnect took {took:.1f}s to reach the proxy"
    assert any(m.get("type") == "http.response.start" and m.get("status") == 499 for m in sent)
