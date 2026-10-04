"""Run a synchronous MCP tool dispatch OFF the asyncio event loop.

``build_mcp_server``'s ``call_tool`` handler is ``async``, but every tool is a
synchronous function (DB queries, and since fed1004 a bounded wait on a live
federated fan-out of up to ``MCP_FEDERATED_LIVE_BUDGET_S``). Called directly on
the loop, a tool blocks EVERY other request the process serves for as long as
it runs — and prod runs ``uvicorn --workers 1``, so that is the whole API,
``/api/healthz`` included.

``asyncio.to_thread`` moves the call to the default executor and copies the
caller's contextvars into it, so request-scoped state resolved before the hop
stays visible. The DB session is opened and closed INSIDE the worker thread: a
session is bound to the thread that uses it, never shared across the hop.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from sqlalchemy.orm import Session

Dispatch = Callable[[str, Session, dict[str, Any], dict[str, Any]], Any]


async def dispatch_off_loop(
    dispatch: Dispatch,
    name: str,
    db_factory: Callable[[], Session],
    arguments: dict[str, Any] | None,
    caller: dict[str, Any],
) -> Any:
    """Run ``dispatch(name, db, arguments, caller)`` in a worker thread.

    A tool error comes back as ``{"error", "tool"}`` — MCP tool failures must
    return an error payload, never crash the transport.
    """

    def _run() -> Any:
        db = db_factory()
        try:
            return dispatch(name, db, arguments or {}, caller)
        finally:
            db.close()

    try:
        return await asyncio.to_thread(_run)
    # Rationale: MCP tool dispatch errors must return error dict, not crash the transport
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "tool": name}
