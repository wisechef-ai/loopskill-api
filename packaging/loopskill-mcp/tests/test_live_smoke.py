"""Live smoke test — proves ``loopskill-mcp`` really handshakes against
https://app.loopskill.io and lists the real tool catalog.

Marked ``network`` and skipped unless ``LOOPSKILL_MCP_LIVE_TEST=1`` is set,
so it never gates a normal ``pytest`` run or CI (this repo's root
``conftest.py`` also blocks non-loopback network from ordinary test runs —
this test deliberately opts out of that guard via the ``network`` marker,
matching the convention already used elsewhere in this repo, e.g.
``pytest.mark.network`` in the root pyproject.toml).

Run explicitly with::

    LOOPSKILL_MCP_LIVE_TEST=1 pytest -m network packaging/loopskill-mcp/tests/test_live_smoke.py -v
"""

from __future__ import annotations

import os

import anyio
import pytest

from loopskill_mcp.config import BridgeConfig
from loopskill_mcp.remote import RemoteMCPClient

pytestmark = pytest.mark.network

LIVE = os.environ.get("LOOPSKILL_MCP_LIVE_TEST") == "1"
SKIP_REASON = "set LOOPSKILL_MCP_LIVE_TEST=1 to run the live handshake smoke test"


@pytest.mark.skipif(not LIVE, reason=SKIP_REASON)
def test_live_initialize_and_list_tools() -> None:
    """Anonymous initialize + tools/list against the real hosted endpoint.

    Run as a plain sync test driving ``anyio.run`` directly (rather than an
    ``async def`` test under pytest-asyncio) — the pytest-asyncio event-loop
    fixture teardown was observed to race the streamable-http client's own
    task-group exit ("cancel scope in a different task"), which is a
    test-harness interaction, not a bridge bug: the identical async flow
    passes cleanly via ``asyncio.run`` in a bare script (see PR description
    for the standalone-script transcript this mirrors).

    Uses a real ``rec_agent_`` key obtained via the documented Ed25519
    self-registration flow (POST /api/agents/register, no OAuth) rather
    than a bare anonymous call: the live StreamableHTTP transport gate in
    app/mcp/streaming.py requires an ``x-api-key`` starting with ``rec_``
    unconditionally (401 with no key at all) — see the PR body's "promise
    vs. implementation" note.
    """

    async def _run() -> list[str]:
        api_key = os.environ.get("LOOPSKILL_MCP_LIVE_TEST_KEY")
        cfg = BridgeConfig(mcp_url="https://app.loopskill.io/api/mcp/http/", api_key=api_key)
        client = RemoteMCPClient(url=cfg.mcp_url, api_key=cfg.api_key)
        tools = await client.list_tools()
        return [t.name for t in tools]

    names = anyio.run(_run)

    assert "loopskill_bundle_install" in names
    assert "loopskill_search" in names
    # The product docs promise "46 tools" — pin a floor, not the exact
    # count, so this test doesn't flake every time a tool is added.
    assert len(names) >= 40
