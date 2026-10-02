from __future__ import annotations

import json
import tomllib
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_codex_configuration_is_four_lines_and_uses_token_environment() -> None:
    text = (EXAMPLES / "codex-config.toml").read_text(encoding="utf-8").strip()
    config = tomllib.loads(text)

    assert len(text.splitlines()) == 4
    assert config["mcp_servers"]["nebius_bionemo"] == {
        "url": "https://bionemo.example.com/mcp",
        "bearer_token_env_var": "BIONEMO_MCP_TOKEN",
        "tool_timeout_sec": 3600,
    }


def _http_server(filename: str) -> dict[str, object]:
    config = json.loads((EXAMPLES / filename).read_text(encoding="utf-8"))
    server: dict[str, object] = config["mcpServers"]["nebius-bionemo"]
    assert server["type"] == "http"
    assert server["url"] == "https://bionemo.example.com/mcp"
    return server


def test_claude_http_example_uses_claude_environment_interpolation() -> None:
    server = _http_server("claude-mcp.json")
    assert server["headers"] == {"Authorization": "Bearer ${BIONEMO_MCP_TOKEN}"}


def test_cursor_http_example_uses_cursor_environment_interpolation() -> None:
    server = _http_server("cursor-mcp.json")
    assert server["headers"] == {"Authorization": "Bearer ${env:BIONEMO_MCP_TOKEN}"}


def test_stdio_example_runs_the_same_package() -> None:
    config = json.loads((EXAMPLES / "stdio-mcp.json").read_text(encoding="utf-8"))
    server = config["mcpServers"]["nebius-bionemo"]

    assert server["command"] == "uv"
    assert "nebius-bionemo-mcp" in server["args"]
    assert server["env"]["BIONEMO_CATALOG_FILE"].endswith("nim-catalog.json")
