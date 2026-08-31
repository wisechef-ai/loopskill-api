"""Tests for the stdio Server wiring — no network.

These patch ``RemoteMCPClient`` out entirely so the assertions are about
*this package's* proxying logic (list_tools/call_tool handlers dispatch to
the remote client and propagate results/errors), not about the live
LoopSkill deployment.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import mcp.types as types
import pytest

from loopskill_mcp.config import BridgeConfig
from loopskill_mcp.remote import RemoteConnectionError
from loopskill_mcp.server import SERVER_NAME, build_server


def _fake_tool() -> types.Tool:
    return types.Tool(
        name="loopskill_search",
        description="Search skills",
        inputSchema={"type": "object", "properties": {"query": {"type": "string"}}},
    )


async def test_list_tools_proxies_to_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    """server's list_tools handler returns whatever the remote client returns."""
    fake_tools = [_fake_tool()]
    fake_remote = AsyncMock()
    fake_remote.list_tools.return_value = fake_tools
    monkeypatch.setattr("loopskill_mcp.server.RemoteMCPClient", lambda **_kw: fake_remote)

    server = build_server(BridgeConfig(mcp_url="https://example.invalid/api/mcp/http/", api_key=None))
    handler = server.request_handlers[types.ListToolsRequest]
    result = await handler(types.ListToolsRequest(method="tools/list"))

    assert result.root.tools == fake_tools
    fake_remote.list_tools.assert_awaited_once()


async def test_call_tool_proxies_to_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    """server's call_tool handler forwards name+arguments and returns the result."""
    fake_result = types.CallToolResult(content=[types.TextContent(type="text", text="ok")])
    fake_remote = AsyncMock()
    fake_remote.call_tool.return_value = fake_result
    monkeypatch.setattr("loopskill_mcp.server.RemoteMCPClient", lambda **_kw: fake_remote)

    server = build_server(BridgeConfig(mcp_url="https://example.invalid/api/mcp/http/", api_key="rec_x"))
    handler = server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name="loopskill_search", arguments={"query": "memory"}),
    )
    result = await handler(req)

    assert result.root.isError is False
    fake_remote.call_tool.assert_awaited_once_with("loopskill_search", {"query": "memory"})


async def test_list_tools_surfaces_remote_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A RemoteConnectionError from the remote client becomes a tool-error result.

    Server.list_tools's own try/except only catches inside the *decorated*
    call path via the lowlevel Server — verifying at this layer that our
    handler re-raises (rather than swallowing) is the contract this bridge
    promises: failures are visible to the calling agent, not silently empty.
    """
    fake_remote = AsyncMock()
    fake_remote.list_tools.side_effect = RemoteConnectionError("boom")
    monkeypatch.setattr("loopskill_mcp.server.RemoteMCPClient", lambda **_kw: fake_remote)

    server = build_server(BridgeConfig(mcp_url="https://example.invalid/api/mcp/http/", api_key=None))
    handler = server.request_handlers[types.ListToolsRequest]
    with pytest.raises(RemoteConnectionError):
        await handler(types.ListToolsRequest(method="tools/list"))


def test_server_name_matches_documented_identity() -> None:
    """SERVER_NAME matches app/mcp/server.py's SERVER_NAME ('loopskill-mcp')."""
    assert SERVER_NAME == "loopskill-mcp"
