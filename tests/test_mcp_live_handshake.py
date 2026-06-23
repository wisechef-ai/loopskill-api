"""Live-integration regression test for the MCP StreamableHTTP transport.

This test exercises the full SDK handshake against a live recipes-api
deployment to guard against the multi-worker session-affinity regression
that was fixed by splitting the MCP path onto its own single-worker process.

Background:
    The MCP `StreamableHTTPSessionManager` keeps session state in-process.
    If the MCP path is served by a multi-worker uvicorn, the SDK sequence:

        POST  initialize           → worker A creates session
        POST  notifications/init   → worker A or B (POSTs are tolerant)
        GET   /api/mcp/http/       → worker B returns 404 "Session not found"
        POST  tools/list           → 404, "Session terminated"

    breaks for every native MCP client. We fix it by running the MCP path on a
    dedicated single-worker process (see deploy/MCP-DEPLOYMENT.md).

This regression test is environment-gated: it requires
``RECIPES_MCP_LIVE_URL`` and ``RECIPES_MCP_LIVE_KEY`` to be set, otherwise
the entire module is skipped. CI does not run it; staging/production smoke
should.

Recommended invocation:

    RECIPES_MCP_LIVE_URL=https://recipes.wisechef.ai/api/mcp/http/ \\
    RECIPES_MCP_LIVE_KEY=rec_... \\
    pytest tests/test_mcp_live_handshake.py -v
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

LIVE_URL = os.environ.get("RECIPES_MCP_LIVE_URL")
LIVE_KEY = os.environ.get("RECIPES_MCP_LIVE_KEY")

pytestmark = pytest.mark.skipif(
    not (LIVE_URL and LIVE_KEY),
    reason="set RECIPES_MCP_LIVE_URL and RECIPES_MCP_LIVE_KEY to run live MCP tests",
)


@pytest.fixture()
def http_client() -> httpx.Client:
    """Single httpx.Client so all requests share a connection pool."""
    with httpx.Client(timeout=30.0) as client:
        yield client


def _post_init(client: httpx.Client) -> tuple[str, dict]:
    """Send `initialize` and return (session_id, parsed_result)."""
    assert LIVE_URL and LIVE_KEY
    resp = client.post(
        LIVE_URL,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "x-api-key": LIVE_KEY,
        },
        content=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "regression-test", "version": "1"},
                },
            }
        ),
    )
    assert resp.status_code == 200, f"initialize failed: {resp.status_code} {resp.text}"
    sid = resp.headers.get("mcp-session-id")
    assert sid, "server did not return Mcp-Session-Id header"
    return sid, resp.text


def _post_initialized(client: httpx.Client, sid: str) -> None:
    """Send the `notifications/initialized` notification."""
    assert LIVE_URL and LIVE_KEY
    resp = client.post(
        LIVE_URL,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "x-api-key": LIVE_KEY,
            "Mcp-Session-Id": sid,
        },
        content=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
    )
    assert resp.status_code == 202, (
        f"notifications/initialized expected 202, got "
        f"{resp.status_code}: {resp.text}"
    )


def _attempt_get_stream(client: httpx.Client, sid: str) -> int:
    """Open the server-initiated GET stream briefly.

    Returns the HTTP status code. We accept the connection being held open
    (httpx ReadTimeout = stream is alive) as success. The critical assertion
    is that the GET does NOT return 404 with "Session not found", which is
    the multi-worker failure mode this test guards against.
    """
    assert LIVE_URL and LIVE_KEY
    try:
        with client.stream(
            "GET",
            LIVE_URL,
            headers={
                "Accept": "text/event-stream",
                "x-api-key": LIVE_KEY,
                "Mcp-Session-Id": sid,
            },
            timeout=2.0,
        ) as resp:
            # If the server immediately responds with 404, we read enough of
            # the body to assert on the message.
            if resp.status_code == 404:
                body = resp.read().decode("utf-8", errors="replace")
                pytest.fail(
                    f"GET stream returned 404 — session-affinity regression. "
                    f"Body: {body!r}"
                )
            return resp.status_code
    except httpx.ReadTimeout:
        # Long-lived stream — server held the connection. That's the success
        # case for an idle SSE channel.
        return 200


def _post_tools_list(client: httpx.Client, sid: str) -> dict:
    """Issue `tools/list` and parse the SSE-wrapped JSON-RPC result."""
    assert LIVE_URL and LIVE_KEY
    resp = client.post(
        LIVE_URL,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "x-api-key": LIVE_KEY,
            "Mcp-Session-Id": sid,
        },
        content=json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    )
    assert resp.status_code == 200, (
        f"tools/list failed after GET stream — session was killed. "
        f"Status: {resp.status_code} Body: {resp.text!r}"
    )
    # The body is SSE-framed: `event: message\r\ndata: {...}\r\n\r\n`
    for line in resp.text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            return json.loads(line[len("data:") :].strip())
    pytest.fail(f"no `data:` line in tools/list SSE response: {resp.text!r}")


def test_healthz_reachable(http_client: httpx.Client) -> None:
    """The /api/mcp/healthz endpoint should be served by the MCP process."""
    assert LIVE_URL
    base = LIVE_URL.rsplit("/http/", 1)[0]
    resp = http_client.get(f"{base}/healthz")
    assert resp.status_code == 200, f"healthz: {resp.status_code} {resp.text}"
    body = resp.json()
    assert body["name"] == "recipes-mcp"
    assert isinstance(body.get("tools"), list)
    assert body["tools"], "healthz reported zero tools"


def test_full_sdk_handshake_survives_get_stream(http_client: httpx.Client) -> None:
    """Regression: the full POST→POST→GET→POST sequence must succeed.

    This is the exact sequence the MCP Python SDK 1.4+ uses on client init.
    On a multi-worker deployment without session affinity, the GET hits a
    different worker than the initialize POST and the server returns 404
    "Session not found", killing the session for all subsequent calls.
    """
    sid, _ = _post_init(http_client)
    _post_initialized(http_client, sid)

    # The GET must NOT return 404. Either it streams (httpx ReadTimeout) or
    # it returns 200 — both are acceptable. 404 is the regression signal.
    status = _attempt_get_stream(http_client, sid)
    assert status != 404, (
        "GET stream returned 404 — multi-worker session-affinity regression"
    )

    # The session must remain alive after the GET. tools/list must succeed
    # and return a non-empty tool catalogue.
    payload = _post_tools_list(http_client, sid)
    assert payload.get("error") is None, f"tools/list returned error: {payload}"
    tools = payload.get("result", {}).get("tools", [])
    assert isinstance(tools, list) and len(tools) > 0, (
        f"tools/list returned empty catalogue: {payload}"
    )
    # Sanity check: the canonical recipes tools should be present.
    tool_names = {t["name"] for t in tools}
    expected = {"recipes_search", "recipes_install", "recipes_list_cookbook"}
    missing = expected - tool_names
    assert not missing, f"missing expected tools: {missing}"
