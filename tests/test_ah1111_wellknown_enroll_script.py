"""ah_1111 — the MCP discovery documents advertise the one-command enroller.

Live 11-11: /.well-known/mcp.json and /.well-known/mcp/server-card.json named
only POST /api/agents/register (hand-built Ed25519 signature). enroll.sh was
named only in agent.json and the 401 breadcrumb, so an MCP client — which reads
mcp.json or the server card, never agent.json — landed on the protocol and not
the client. What is pinned here:

1. Both MCP documents carry ``authentication.enroll_script``.
2. It is the SAME block agent.json carries (one builder, no drift).
3. The URL it names is a route this app serves, and the script body there
   ends in the same first-install command the block advertises.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.agent_enroll_script_routes import ENROLL_SCRIPT_PATH, FIRST_INSTALL_BUNDLE
from app.main import create_app

MCP_DOCS = ("/.well-known/mcp.json", "/.well-known/mcp/server-card.json")


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app())


@pytest.mark.parametrize("path", MCP_DOCS)
def test_mcp_documents_name_the_enroller(client, path):
    auth = client.get(path).json()["authentication"]
    script = auth["enroll_script"]
    assert script["url"].endswith(ENROLL_SCRIPT_PATH)
    assert script["usage"].endswith(f"{ENROLL_SCRIPT_PATH} | bash -s -- <agent-name> [contact]")
    assert script["next_step"]["requires_key"] is False
    assert script["next_step"]["bundle"] == FIRST_INSTALL_BUNDLE
    # The protocol stays advertised next to the client, not replaced by it.
    assert auth["self_registration"].endswith("/api/agents/register")


@pytest.mark.parametrize("path", MCP_DOCS)
def test_mcp_documents_and_agent_json_carry_one_block(client, path):
    agent = client.get("/.well-known/agent.json").json()["registration"]["enroll_script"]
    assert client.get(path).json()["authentication"]["enroll_script"] == agent


def test_advertised_enroller_is_served_and_ends_in_the_advertised_install(client):
    script = client.get("/.well-known/mcp.json").json()["authentication"]["enroll_script"]
    path = "/" + script["url"].split("://", 1)[1].split("/", 1)[1]
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code)
    tail = script["next_step"]["command"].split("/api/bundles/install.sh", 1)[1]
    assert f"$API_BASE/api/bundles/install.sh{tail}" in r.text


def test_enroller_block_carries_no_key_material(client):
    for path in MCP_DOCS:
        body = json.dumps(client.get(path).json()["authentication"])
        for marker in ("rec_agent_", "lsk_", "sk_live", "sk_test", "Bearer "):
            assert marker not in body, (path, marker)
