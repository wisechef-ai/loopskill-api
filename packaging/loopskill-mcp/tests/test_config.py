"""Tests for env-var resolution — no network."""

from __future__ import annotations

from loopskill_mcp.config import (
    DEFAULT_MCP_URL,
    load_config,
    resolve_api_key,
)


def test_resolve_api_key_canonical() -> None:
    """LOOPSKILL_API_KEY (canonical, per docs/SELF_HOST.md) wins outright."""
    assert resolve_api_key({"LOOPSKILL_API_KEY": "rec_live_abc"}) == "rec_live_abc"


def test_resolve_api_key_canonical_beats_legacy() -> None:
    """Canonical name wins even when a legacy var is also set."""
    env = {"LOOPSKILL_API_KEY": "rec_live_new", "RECIPES_API_KEY": "rec_live_old"}
    assert resolve_api_key(env) == "rec_live_new"


def test_resolve_api_key_legacy_fallback() -> None:
    """RECIPES_API_KEY keeps working per SELF_HOST.md's transition promise."""
    assert resolve_api_key({"RECIPES_API_KEY": "rec_live_legacy"}) == "rec_live_legacy"


def test_resolve_api_key_mcp_wizard_var() -> None:
    """MCP_LOOPSKILL_API_KEY (Hermes wizard var) is honoured."""
    assert resolve_api_key({"MCP_LOOPSKILL_API_KEY": "rec_live_wizard"}) == "rec_live_wizard"


def test_resolve_api_key_anonymous_is_valid() -> None:
    """No key at all resolves to None — anonymous/free-tier mode, not an error."""
    assert resolve_api_key({}) is None


def test_resolve_api_key_ignores_empty_string() -> None:
    """An exported-but-empty var must not shadow anonymous mode."""
    assert resolve_api_key({"LOOPSKILL_API_KEY": ""}) is None


def test_load_config_default_url() -> None:
    """Default MCP URL is the documented hosted StreamableHTTP endpoint."""
    cfg = load_config({})
    assert cfg.mcp_url == DEFAULT_MCP_URL
    assert cfg.mcp_url == "https://app.loopskill.io/api/mcp/http/"
    assert cfg.api_key is None


def test_load_config_self_host_override() -> None:
    """LOOPSKILL_MCP_URL lets a self-hosted deployment override the endpoint."""
    cfg = load_config({"LOOPSKILL_MCP_URL": "https://your-host/api/mcp/http/"})
    assert cfg.mcp_url == "https://your-host/api/mcp/http/"


def test_load_config_full() -> None:
    """API key + custom URL both resolve together."""
    cfg = load_config(
        {
            "LOOPSKILL_API_KEY": "rec_live_x",
            "LOOPSKILL_MCP_URL": "https://your-host/api/mcp/http/",
        }
    )
    assert cfg.api_key == "rec_live_x"
    assert cfg.mcp_url == "https://your-host/api/mcp/http/"
