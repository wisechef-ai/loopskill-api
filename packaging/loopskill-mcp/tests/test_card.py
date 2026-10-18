"""fetch_card_tools: parse the public server card into mcp Tool objects (no network)."""

from __future__ import annotations

import httpx
import pytest

from loopskill_mcp import remote
from loopskill_mcp.config import BridgeConfig, load_config
from loopskill_mcp.remote import RemoteConnectionError, fetch_card_tools

CARD = {
    "serverInfo": {"name": "loopskill-mcp"},
    "tools": [
        {"name": "skill_search", "description": "Search", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "bundle_list", "inputSchema": {"type": "object"}},
        "not-a-dict",
    ],
}


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    real = httpx.AsyncClient

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    monkeypatch.setattr(remote.httpx, "AsyncClient", factory)


async def test_parses_tools_from_card(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_transport(monkeypatch, lambda req: httpx.Response(200, json=CARD))
    tools = await fetch_card_tools("https://h.example/.well-known/mcp/server-card.json")
    assert [t.name for t in tools] == ["skill_search", "bundle_list"]
    assert tools[0].inputSchema == {"type": "object", "properties": {}}


@pytest.mark.parametrize(
    "resp",
    [
        httpx.Response(503, text="down"),
        httpx.Response(200, json={"tools": []}),
        httpx.Response(200, json={"no_tools": 1}),
        httpx.Response(200, text="<html>not json"),
    ],
)
async def test_bad_card_raises(monkeypatch: pytest.MonkeyPatch, resp: httpx.Response) -> None:
    _patch_transport(monkeypatch, lambda req: resp)
    with pytest.raises(RemoteConnectionError):
        await fetch_card_tools("https://h.example/.well-known/mcp/server-card.json")


def test_card_and_register_urls_derive_from_mcp_origin() -> None:
    cfg = BridgeConfig(mcp_url="https://self.host:8443/api/mcp/http/", api_key=None)
    assert cfg.card_url == "https://self.host:8443/.well-known/mcp/server-card.json"
    assert cfg.register_url == "https://self.host:8443/api/agents/register"


def test_card_url_env_override() -> None:
    cfg = load_config({"LOOPSKILL_SERVER_CARD_URL": "https://cdn.example/card.json"})
    assert cfg.card_url == "https://cdn.example/card.json"
    assert load_config({}).card_url == "https://app.loopskill.io/.well-known/mcp/server-card.json"
