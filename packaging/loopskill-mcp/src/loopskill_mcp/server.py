"""stdio MCP server that proxies every request to the hosted LoopSkill endpoint.

This is the piece that makes ``uvx loopskill-mcp`` behave like a real local
MCP server from a client's point of view (Claude Desktop, Hermes, Codex CLI,
etc. all speak stdio to it), while every actual tool call is forwarded to
the hosted StreamableHTTP endpoint at ``https://app.loopskill.io/api/mcp/http/``
— the same server ``app/mcp/server.py`` builds, just reached over HTTP
instead of in-process, because an end user has no local database or app
config to run that module directly.
"""

from __future__ import annotations

import logging

import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

from loopskill_mcp import __version__
from loopskill_mcp.config import BridgeConfig, load_config
from loopskill_mcp.remote import RemoteConnectionError, RemoteMCPClient

logger = logging.getLogger("loopskill_mcp")

SERVER_NAME = "loopskill-mcp"


def build_server(config: BridgeConfig) -> Server:
    """Build the lowlevel stdio Server, wiring its handlers to the remote client."""
    server: Server = Server(SERVER_NAME, version=__version__)
    remote = RemoteMCPClient(
        url=config.mcp_url,
        api_key=config.api_key,
        client_info=types.Implementation(name=SERVER_NAME, version=__version__),
    )

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        try:
            return await remote.list_tools()
        except RemoteConnectionError:
            logger.exception("tools/list: could not reach hosted LoopSkill endpoint")
            raise

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> types.CallToolResult:
        try:
            return await remote.call_tool(name, arguments)
        except RemoteConnectionError:
            logger.exception("tools/call %s: could not reach hosted LoopSkill endpoint", name)
            raise

    return server


async def run_stdio(config: BridgeConfig | None = None) -> None:
    """Serve the bridge over stdio until the client disconnects."""
    cfg = config or load_config()
    server = build_server(cfg)
    init_options = InitializationOptions(
        server_name=SERVER_NAME,
        server_version=__version__,
        capabilities=server.get_capabilities(
            notification_options=NotificationOptions(),
            experimental_capabilities={},
        ),
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_options)
