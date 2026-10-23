"""Optional admitting-key filter; ordinary principal-scoped listing is unchanged."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app
import attempts
import config
import db
import jobs
import keys
from test_api import client, cluster  # noqa: F401
from test_database import database  # noqa: F401
from test_operations_list import NS, P1A, P1B, P2, _all_pages, _expire, _ids, _invoke, _rig


def entry(name, *, fingerprint=None, root=None, durable=False):
    if durable:
        return ("2026-10-15T10:00:00Z", name, None, {"root": root or name})
    annotations = {}
    if fingerprint is not None:
        annotations[f"{config.LABEL}/key"] = fingerprint
    if root:
        annotations[f"{config.LABEL}/resumed-from"] = root
    return ("2026-10-15T10:00:00Z", name, {"metadata": {"name": name, "annotations": annotations}}, None)


def test_live_annotation_filters_exact_key_not_principal(monkeypatch):
    monkeypatch.setattr(keys, "admitting_key_hashes", lambda _: {})
    own = entry("own", fingerprint=keys.key_hash("first-key"))
    sibling = entry("sibling", fingerprint=keys.key_hash("second-key"))
    assert app._created_by_current_key([sibling, own], SimpleNamespace(key="first-key")) == [own]


def test_durable_continuation_inherits_original_admission_not_reading_key(monkeypatch):
    root_hash = keys.key_hash("original")
    monkeypatch.setattr(keys, "admitting_key_hashes", lambda names: {"root": {root_hash}})
    row = entry("root-r1", root="root", durable=True)
    assert app._created_by_current_key([row], SimpleNamespace(key="original")) == [row]
    assert app._created_by_current_key([row], SimpleNamespace(key="another")) == []


@pytest.mark.parametrize("kind", ["missing", "malformed", "conflicting", "database"])
def test_unknown_attribution_is_unavailable_without_private_identifiers(monkeypatch, kind):
    evidence = {}
    if kind == "conflicting":
        evidence = {"private-operation": {keys.key_hash("other")}}

    def lookup(_):
        if kind == "database":
            raise OSError("private connection string")
        return evidence

    monkeypatch.setattr(keys, "admitting_key_hashes", lookup)
    row = entry(
        "private-operation",
        fingerprint=(None if kind == "missing" else "malformed" if kind == "malformed" else keys.key_hash("own")),
    )
    with pytest.raises(HTTPException) as error:
        app._created_by_current_key([row], SimpleNamespace(key="own"))
    assert error.value.status_code == 503
    assert error.value.detail == "complete operation attribution is unavailable"


def test_unknown_outside_requested_page_is_not_hidden(monkeypatch):
    monkeypatch.setattr(keys, "admitting_key_hashes", lambda _: {})
    with pytest.raises(HTTPException) as error:
        app._created_by_current_key(
            [entry("new", fingerprint=keys.key_hash("own")), entry("old")], SimpleNamespace(key="own")
        )
    assert error.value.status_code == 503


def test_bulk_helper_keeps_conflicts_and_uses_only_requested_operations(database):
    db.migrate()
    with db.pool().connection() as c:
        c.execute(
            "insert into key_run_members(operation,root,key_hash,tenant) values (%s,%s,%s,%s)",
            ("kept", "kept", keys.key_hash("first"), "demo"),
        )
        c.execute(
            "insert into key_reservations(operation,key_hash,tenant) values (%s,%s,%s)",
            ("kept", keys.key_hash("second"), "demo"),
        )
        c.execute(
            "insert into key_reservations(operation,key_hash,tenant) values (%s,%s,%s)",
            ("foreign", keys.key_hash("third"), "other"),
        )
    assert keys.admitting_key_hashes({"kept", "absent"}) == {"kept": {keys.key_hash("first"), keys.key_hash("second")}}


def test_real_same_principal_keys_filter_before_paging_and_survive_job_ttl(database, client, monkeypatch):
    c, fake = _rig(database, client, monkeypatch)
    own = _invoke(c, P1A, 3)
    sibling = _invoke(c, P1B, 4)
    foreign = _invoke(c, P2)[0]
    _expire(c, fake, own[0], "2026-10-14T09:00:00Z", "2026-10-14T09:30:00Z")
    # Settlement removes the reservation; the original admitting key is still durable.
    keys.settle_run_member(own[0], 0)
    mine, pages = _all_pages(c, P1A, 1, created_by_current_key="true")
    assert set(mine) == set(own) and len(mine) == 3 and pages == 3
    assert set(_ids(c.get("/v1/operations", headers=P1A))) == set(own + sibling)
    assert foreign not in mine
    theirs, _ = _all_pages(c, P1B, 2, created_by_current_key="true")
    assert set(theirs) == set(sibling)
    response = c.get("/v1/operations", headers=P1A, params={"created_by_current_key": True})
    assert response.headers["x-created-by-current-key"] == "true"
    assert "key_hash" not in response.text and keys.key_hash("sk-p1b") not in response.text


def test_filter_refuses_unknown_but_default_listing_is_unchanged(database, client, monkeypatch):
    c, fake = _rig(database, client, monkeypatch)
    op = _invoke(c, P1A)[0]
    fake.jobs[(NS, op)]["metadata"]["annotations"].pop(f"{config.LABEL}/key")
    monkeypatch.setattr(keys, "admitting_key_hashes", lambda _: {})
    assert _ids(c.get("/v1/operations", headers=P1A)) == [op]
    response = c.get("/v1/operations", headers=P1A, params={"created_by_current_key": True})
    assert response.status_code == 503 and op not in response.text


def test_filter_keeps_model_and_scope_denials(database, client, monkeypatch):
    c, fake = _rig(database, client, monkeypatch)
    _invoke(c, P1A)
    # Existing ownership grants run first, even if a foreign record carries our key annotation.
    foreign = _invoke(c, P2)[0]
    fake.jobs[(NS, foreign)]["metadata"]["annotations"][f"{config.LABEL}/key"] = keys.key_hash("sk-p1a")
    response = c.get("/v1/operations", headers=P1A, params={"created_by_current_key": True})
    assert foreign not in _ids(response)


@pytest.mark.parametrize("missing", ["region", "records"])
def test_filtered_list_never_claims_an_incomplete_inventory_is_complete(database, client, monkeypatch, missing):
    c, _ = _rig(database, client, monkeypatch)
    _invoke(c, P1A)
    if missing == "region":
        original = jobs.list_ops_all
        monkeypatch.setattr(jobs, "list_ops_all", lambda *args: (original(*args)[0], ["other-region"]))
    else:

        def unavailable(*args):
            raise OSError("private database diagnostic")

        monkeypatch.setattr(attempts, "durable_index", unavailable)
    ordinary = c.get("/v1/operations", headers=P1A)
    assert ordinary.status_code == 200 and "x-created-by-current-key" not in ordinary.headers
    strict = c.get("/v1/operations", headers=P1A, params={"created_by_current_key": True})
    assert strict.status_code == 503
    assert strict.json() == {"detail": "complete operation attribution is unavailable"}
