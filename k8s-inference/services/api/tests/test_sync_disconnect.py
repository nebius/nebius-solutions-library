"""A proxied synchronous call ends when its caller goes away (api 0.11.127, docs/API.md "Synchronous calls"): the
downstream request is cancelled, the client closed, 499 raised so the caller's `finally` releases the admission; a
timeout still answers 504; a normal answer is returned untouched."""

import asyncio

import httpx
import pytest
from fastapi import HTTPException

import app as appmod


class _Request:
    def __init__(self, disconnect_after: int):
        self.calls = 0
        self.disconnect_after = disconnect_after

    async def is_disconnected(self):
        self.calls += 1
        return self.calls >= self.disconnect_after


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
    req = _Request(disconnect_after=2)

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
