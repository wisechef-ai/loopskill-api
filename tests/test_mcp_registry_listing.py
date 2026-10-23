"""MCP Registry listing (server.json + publish workflow) stays truthful.

The hosted endpoint is remote-only in the registry: the PyPI ``loopskill-mcp``
README is frozen per release, so it cannot carry the ``mcp-name`` ownership
marker until a new release is cut.  These tests pin the facts server.json
declares to what the app actually enforces, so the listing cannot drift.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SERVER = json.loads((ROOT / "server.json").read_text())
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/publish-mcp-registry.yml").read_text())


def test_name_is_github_org_namespace():
    # GitHub OIDC grants io.github.<repository_owner>/* only.
    assert re.fullmatch(r"io\.github\.wisechef-ai/[a-zA-Z0-9._-]+", SERVER["name"])
    assert SERVER["$schema"].startswith("https://static.modelcontextprotocol.io/schemas/")


def test_registry_field_limits():
    assert 1 <= len(SERVER["description"]) <= 100
    assert 1 <= len(SERVER["title"]) <= 100
    assert re.fullmatch(r"\d+\.\d+\.\d+", SERVER["version"])  # no ranges


def test_remote_matches_mounted_endpoint_and_auth_header():
    (remote,) = SERVER["remotes"]
    assert remote["type"] == "streamable-http"
    # Trailing slash: the bare path answers 307.
    assert remote["url"] == "https://app.loopskill.io/api/mcp/http/"
    (header,) = remote["headers"]
    assert header["name"] == "x-api-key"
    assert header["isRequired"] is True and header["isSecret"] is True
    # The header name must be the one the MCP route actually reads.
    streaming = (ROOT / "app/mcp/streaming.py").read_text()
    assert f'request.headers.get("{header["name"]}")' in streaming
    # Honest listing: no anonymous-access claim next to a required key.
    assert "anonymous" not in json.dumps(SERVER).lower()


def test_remote_only_no_unverifiable_package_entry():
    assert "packages" not in SERVER


def test_publish_workflow_is_oidc_manual_and_pinned():
    triggers = WORKFLOW.get("on", WORKFLOW.get(True))
    assert set(triggers) == {"workflow_dispatch"}
    job = WORKFLOW["jobs"]["publish"]
    assert job["permissions"]["id-token"] == "write"
    assert job["permissions"]["contents"] == "read"
    assert job["if"] == "github.ref == 'refs/heads/main'"
    text = (ROOT / ".github/workflows/publish-mcp-registry.yml").read_text()
    assert "mcp-publisher login github-oidc" in text
    assert "releases/latest" not in text  # pinned, not the moving `latest` alias
    assert re.search(r'MCP_PUBLISHER_SHA256: "[0-9a-f]{64}"', text)


def test_packaging_readme_does_not_claim_anonymous_mcp():
    readme = (ROOT / "packaging/loopskill-mcp/README.md").read_text()
    assert "works anonymously" not in readme
    assert "install\nwith no key at all" not in readme


def test_packaging_readme_carries_registry_mcp_name_marker():
    """The MCP Registry verifies PyPI ownership by an `mcp-name: <server name>` line in the
    PUBLISHED package README. It must ship inside the wheel's README before the first stdio
    `packages[]` entry is registered, or the registry publish fails and needs a re-release."""
    name = json.loads((ROOT / "server.json").read_text())["name"]
    readme = (ROOT / "packaging/loopskill-mcp/README.md").read_text()
    assert f"mcp-name: {name}" in readme
