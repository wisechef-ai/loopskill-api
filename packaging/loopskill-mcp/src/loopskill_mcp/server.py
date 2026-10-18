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
from loopskill_mcp.remote import RemoteConnectionError, RemoteMCPClient, fetch_card_tools

logger = logging.getLogger("loopskill_mcp")

SERVER_NAME = "loopskill-mcp"


def keyless_call_error(name: str, register_url: str) -> types.CallToolResult:
    """The tools/call answer when no API key is configured and the host 401s."""
    text = (
        f"LoopSkill tool '{name}' needs a free API key. Get one (no signup form): "
        f"POST {register_url} , then set LOOPSKILL_API_KEY in this MCP server's env "
        "and restart your client. tools/list works without a key."
    )
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], isError=True)


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
            if config.api_key:
                # A configured key that fails is a real error (bad key, outage);
                # never mask it with the public catalogue.
                logger.exception("tools/list: could not reach hosted LoopSkill endpoint")
                raise
            # Keyless: the hosted endpoint 401s by design (#393). Serve the
            # public server card instead, so directory checks and first-time
            # users see the real catalogue. If the card fails too, raise.
            logger.info("tools/list: no API key; serving tools from %s", config.card_url)
            return await fetch_card_tools(config.card_url)

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> types.CallToolResult:
        try:
            return await remote.call_tool(name, arguments)
        except RemoteConnectionError:
            if not config.api_key:
                return keyless_call_error(name, config.register_url)
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
