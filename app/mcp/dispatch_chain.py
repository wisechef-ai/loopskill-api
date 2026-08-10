"""fleetos_1607 — delegated MCP dispatch chain.

Extracted from app/mcp/server.py to keep that module under the 600-line
god-object gate. Each delegated dispatcher (fleet write-surface F1, placements,
harvest) returns the shared _NOT_HANDLED sentinel when it doesn't own the tool;
``run_dispatch_chain`` walks them in order and returns the first real result.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.auth_ctx import AuthContext
from app.mcp.tools.fleet_write import _NOT_HANDLED, dispatch_f1
from app.mcp.tools.fleet_ingest import dispatch_ingest
from app.mcp.tools.federation_propose import dispatch_federation_propose
from app.mcp.tools.harvest import dispatch_harvest
from app.mcp.tools.placement import dispatch_placement

# Ordered chain of delegated dispatchers. Append future phase dispatchers here
# rather than growing server.py's _dispatch god node.
_CHAIN = (
    dispatch_f1,
    dispatch_placement,
    dispatch_harvest,
    dispatch_ingest,
    dispatch_federation_propose,
)


def run_dispatch_chain(name: str, db: Session, args: dict[str, Any], ctx: AuthContext) -> Any:
    """Walk the delegated-dispatch chain; return the first non-sentinel result.

    Returns _NOT_HANDLED if no dispatcher owns ``name`` (caller raises).
    """
    for handler in _CHAIN:
        result = handler(name, db, args, ctx)
        if result is not _NOT_HANDLED:
            return result
    return _NOT_HANDLED


# Re-export the sentinel so callers can compare without importing fleet_write.
DISPATCH_NOT_HANDLED = _NOT_HANDLED
