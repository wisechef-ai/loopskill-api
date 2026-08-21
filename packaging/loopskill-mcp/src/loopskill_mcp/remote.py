"""Client to the hosted LoopSkill StreamableHTTP MCP endpoint — one connection per call.

The whole point of this package: end users cannot run ``app/mcp/server.py``
directly (it needs the full FastAPI app + a live database). This module is
the other half of the bridge — it speaks the real MCP *client* protocol to
``https://app.loopskill.io/api/mcp/http/`` over HTTP, so the stdio server in
``loopskill_mcp.server`` can proxy a local agent's stdio calls to it.

Each call opens a fresh MCP session (transport connect + initialize) and
tears it down before returning. This costs an extra round-trip per call
versus a persistent session, but a persistent ``AsyncExitStack`` entered in
one task and exited in another (e.g. at process shutdown, when the lowlevel
``mcp.server.lowlevel.Server`` may run each request in its own anyio task)
hits anyio's "cancel scope exited in a different task" failure mode — opening
and closing symmetrically within one call keeps every enter/exit in the same
task, which is the actual fix (observed live via a real ``uvx`` stdio
session; see the PR body's shutdown-teardown note).
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from typing import Any

import mcp.types as types
from mcp import ClientSession

# ``streamablehttp_client`` is the function that accepts a ``headers`` dict
# (needed to send x-api-key) — the newer PEP8-named ``streamable_http_client``
# introduced alongside it dropped the ``headers`` param in favour of a
# caller-supplied ``http_client``, so it is NOT a drop-in replacement here.
# Pin to the header-capable name explicitly rather than "prefer the newest
# name available", which would silently break auth on an SDK upgrade.
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger("loopskill_mcp")


class RemoteConnectionError(RuntimeError):
    """Raised when the bridge cannot reach or handshake with the hosted endpoint."""


class RemoteMCPClient:
    """Connects to the hosted StreamableHTTP endpoint fresh for every call.

    Stateless from the caller's point of view: :meth:`list_tools` and
    :meth:`call_tool` each own their connection's full lifecycle (connect,
    initialize, use, close) within a single ``async with`` block.
    """

    def __init__(
        self,
        url: str,
        api_key: str | None,
        client_info: types.Implementation | None = None,
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._client_info = client_info

    async def _run(self, fn: Any) -> Any:
        headers = {"x-api-key": self._api_key} if self._api_key else None
        try:
            async with AsyncExitStack() as stack:
                read, write, _get_session_id = await stack.enter_async_context(
                    streamablehttp_client(self._url, headers=headers)
                )
                session = await stack.enter_async_context(
                    ClientSession(read, write, client_info=self._client_info)
                )
                await session.initialize()
                return await fn(session)
        except RemoteConnectionError:
            raise
        except Exception as exc:
            # Rationale: any transport/handshake/protocol failure against the
            # remote endpoint must surface as a typed RemoteConnectionError
            # to the caller (which reports it back over stdio as a tool/init
            # error), not crash the whole stdio process.
            raise RemoteConnectionError(f"failed to reach {self._url}: {exc}") from exc

    async def list_tools(self) -> list[types.Tool]:
        """Proxy ``tools/list`` to the hosted endpoint."""

        async def _call(session: ClientSession) -> list[types.Tool]:
            result = await session.list_tools()
            return result.tools

        return await self._run(_call)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        """Proxy ``tools/call`` to the hosted endpoint."""

        async def _call(session: ClientSession) -> types.CallToolResult:
            return await session.call_tool(name, arguments)

        return await self._run(_call)
