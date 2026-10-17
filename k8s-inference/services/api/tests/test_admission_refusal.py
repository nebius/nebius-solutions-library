"""A ledger refusal, unlike an endpoint429, proves that this request was not admitted."""

import pytest
from fastapi import HTTPException

import app as appmod
import keys
from auth import Principal


@pytest.mark.parametrize("code", [402, 403, 429])
def test_only_ledger_throttle_emits_refusal_header(monkeypatch, code):
    monkeypatch.setattr(appmod.db, "enabled", lambda: True)

    def refuse(*args):
        raise keys.KeyLimit(code, "synthetic refusal")

    monkeypatch.setattr(keys, "admit", refuse)
    with pytest.raises(HTTPException) as failure:
        appmod._admit(Principal("synthetic-key", "system", {}), "op-synthetic")
    assert failure.value.status_code == code
    assert failure.value.headers == ({"X-Serverless2-Admission": "refused"} if code == 429 else None)


def test_successful_admission_is_not_reinterpreted(monkeypatch):
    monkeypatch.setattr(appmod.db, "enabled", lambda: True)
    expected = {"replay": False}
    monkeypatch.setattr(keys, "admit", lambda *args: expected)
    assert appmod._admit(Principal("synthetic-key", "system", {}), "op-synthetic") is expected
