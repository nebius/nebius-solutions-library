from __future__ import annotations

import hashlib
import json

import pytest

from nebius_bionemo_mcp.download import download


def _result(source, *, name: str = "structure.cif", size_delta: int = 0, digest: str | None = None) -> dict:
    content = source.read_bytes()
    return {
        "artifacts": [
            {
                "name": name,
                "download_url": source.as_uri(),
                "size_bytes": len(content) + size_delta,
                "sha256": digest or hashlib.sha256(content).hexdigest(),
            }
        ]
    }


def test_download_verifies_local_artifact(tmp_path) -> None:
    source = tmp_path / "source.cif"
    source.write_bytes(b"data_TEST")
    result = tmp_path / "result.json"
    result.write_text(json.dumps(_result(source)), encoding="utf-8")

    paths = download(result, tmp_path / "run")

    assert paths[0].read_bytes() == b"data_TEST"


@pytest.mark.parametrize(
    ("size_delta", "digest", "message"),
    [(1, None, "size mismatch"), (0, "0" * 64, "checksum mismatch")],
)
def test_download_rejects_invalid_artifact(tmp_path, size_delta: int, digest: str | None, message: str) -> None:
    source = tmp_path / "source.cif"
    source.write_bytes(b"data_TEST")
    result = tmp_path / "result.json"
    result.write_text(json.dumps(_result(source, size_delta=size_delta, digest=digest)), encoding="utf-8")
    run = tmp_path / "run"

    with pytest.raises(ValueError, match=message):
        download(result, run)

    assert not list(run.glob("*.part"))


def test_download_rejects_colliding_names_and_nonempty_directory(tmp_path) -> None:
    source = tmp_path / "source.cif"
    source.write_bytes(b"data_TEST")
    result_data = _result(source, name="same/name")
    result_data["artifacts"].append(_result(source, name="same?name")["artifacts"][0])
    result = tmp_path / "result.json"
    result.write_text(json.dumps(result_data), encoding="utf-8")

    with pytest.raises(ValueError, match="collide"):
        download(result, tmp_path / "run")

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "existing").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="not empty"):
        download(result, occupied)
