"""bundles_0811 P3.5 — the registry-proposal MCP tool is reachable via the chain.

WHY THIS FILE EXISTS
--------------------
`loopskill_propose_registry` was originally dispatched by a branch inside
`app/mcp/server.py::_dispatch`. That module sits 10 lines under a 600-line
god-object gate (`test_w0_2_pyfile_size_discipline`, explicitly NEVER waived),
so the new branch pushed it to 603 and failed CI on both pytest jobs.

The fix was to move dispatch into `app/mcp/tools/federation_propose.py` and
register it in `app/mcp/dispatch_chain.py` — which is precisely what that
module's own docstring prescribes: *"Append future phase dispatchers here rather
than growing server.py's _dispatch god node."*

A refactor that silently stops routing a tool is worse than the size violation
it fixed, so these tests pin the ROUTING, not the implementation:

  1. the chain owns the tool name (it is not silently unreachable)
  2. a foreign name falls through to the sentinel (we did not hijack the chain)
  3. the dispatcher is actually registered in the chain tuple
  4. server.py stays under the god-object cap
"""

from __future__ import annotations

from pathlib import Path

from app.auth_ctx import AuthContext
from app.mcp.dispatch_chain import _CHAIN, DISPATCH_NOT_HANDLED
from app.mcp.tools.federation_propose import (
    _FEDERATION_PROPOSE_TOOLS,
    dispatch_federation_propose,
)

SERVER_PY = Path(__file__).resolve().parents[1] / "app" / "mcp" / "server.py"
GOD_OBJECT_LINE_CAP = 600


class TestRegistryProposalIsReachable:
    def test_chain_owns_the_tool_name(self):
        assert "loopskill_propose_registry" in _FEDERATION_PROPOSE_TOOLS

    def test_dispatcher_is_registered_in_the_chain(self):
        # If this fails the tool is unreachable over MCP even though its
        # implementation still exists — the exact silent-break a size refactor
        # can cause.
        assert dispatch_federation_propose in _CHAIN

    def test_foreign_tool_falls_through(self):
        # The handler must not swallow names it does not own, or every tool
        # registered AFTER it in the chain becomes unreachable.
        result = dispatch_federation_propose(
            "loopskill_definitely_not_ours", None, {}, AuthContext(scope="user")
        )
        assert result is DISPATCH_NOT_HANDLED


class TestGodObjectCapHolds:
    def test_server_py_under_cap(self):
        lines = SERVER_PY.read_text().count("\n")
        assert lines <= GOD_OBJECT_LINE_CAP, (
            f"app/mcp/server.py is {lines} lines (cap {GOD_OBJECT_LINE_CAP}). "
            "Add a delegated dispatcher in app/mcp/dispatch_chain.py instead of "
            "another branch in _dispatch."
        )
