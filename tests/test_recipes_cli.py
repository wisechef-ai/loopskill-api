"""Tests for tools/recipes_cli.py — share token creation, config output, flags."""

import json
import sys
from unittest.mock import patch, MagicMock, call
from io import StringIO

import pytest

# Make the tools directory importable
sys.path.insert(0, ".")

from tools.recipes_cli import cmd_share, _print_config_blocks, _get_api_key


# ── Fixtures ──────────────────────────────────────────────────────────

MOCK_COOKBOOK_ID = "2bd74055-fd35-4590-8c53-f46626bdaadc"
MOCK_TOKEN = "cbt_a1b2c3d4_e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
MOCK_TOKEN_ID = "tok_abcdef12"

MOCK_API_RESPONSE = {
    "id": MOCK_TOKEN_ID,
    "token": MOCK_TOKEN,
    "prefix": "cbt_a1b2c3d4",
    "scope": "edit",
    "name": "shared via CLI",
    "created_at": "2025-05-07T12:00:00Z",
}


def _make_args(read_only=False, name=None):
    """Create a mock argparse namespace."""
    return MagicMock(
        cookbook_id=MOCK_COOKBOOK_ID,
        read_only=read_only,
        name=name or "shared via CLI",
    )


# ── Tests ─────────────────────────────────────────────────────────────


class TestConfigBlocks:
    """Verify config block output format."""

    def test_hermes_config_contains_streamable_http(self, capsys):
        _print_config_blocks(MOCK_TOKEN)
        out = capsys.readouterr().out
        assert "streamable-http" in out
        assert f"x-api-key: {MOCK_TOKEN}" in out

    def test_claude_desktop_config_is_valid_json(self, capsys):
        _print_config_blocks(MOCK_TOKEN)
        out = capsys.readouterr().out
        # Extract JSON between first { and the matching closing }
        start = out.index("// ── Claude Desktop")
        json_start = out.index("{", start)
        # Find the matching closing brace by counting depth
        depth = 0
        for i in range(json_start, len(out)):
            if out[i] == "{":
                depth += 1
            elif out[i] == "}":
                depth -= 1
                if depth == 0:
                    json_text = out[json_start : i + 1]
                    break
        parsed = json.loads(json_text)
        assert "mcpServers" in parsed
        srv = parsed["mcpServers"]["recipes-shared"]
        assert srv["url"] == "https://recipes.wisechef.ai/api/mcp/http"
        assert srv["headers"]["x-api-key"] == MOCK_TOKEN
        assert srv["type"] == "streamable-http"

    def test_config_contains_both_clients(self, capsys):
        _print_config_blocks(MOCK_TOKEN)
        out = capsys.readouterr().out
        assert "Hermes config.yaml" in out
        assert "Claude Desktop" in out


class TestShareCommand:
    """Verify share command with mocked API."""

    @patch("tools.recipes_cli._api_post")
    @patch("tools.recipes_cli._get_api_key", return_value="fake-key")
    def test_basic_share(self, mock_key, mock_post, capsys):
        mock_post.return_value = MOCK_API_RESPONSE
        cmd_share(_make_args())
        out = capsys.readouterr().out
        assert "✓ Share token created" in out
        assert MOCK_TOKEN in out
        assert "streamable-http" in out

    @patch("tools.recipes_cli._api_post")
    @patch("tools.recipes_cli._get_api_key", return_value="fake-key")
    def test_read_only_flag(self, mock_key, mock_post, capsys):
        mock_post.return_value = {**MOCK_API_RESPONSE, "scope": "read"}
        cmd_share(_make_args(read_only=True))
        out = capsys.readouterr().out
        assert "Scope:   read" in out
        # Verify the API was called with scope=read
        call_args = mock_post.call_args
        assert call_args[0][1]["scope"] == "read"

    @patch("tools.recipes_cli._api_post")
    @patch("tools.recipes_cli._get_api_key", return_value="fake-key")
    def test_custom_name(self, mock_key, mock_post, capsys):
        custom_name = "My custom label"
        mock_post.return_value = {
            **MOCK_API_RESPONSE,
            "name": custom_name,
        }
        # Use real argparse-like namespace instead of MagicMock for .name
        class RealArgs:
            def __init__(self):
                self.cookbook_id = MOCK_COOKBOOK_ID
                self.read_only = False
                self.name = custom_name
        cmd_share(RealArgs())
        out = capsys.readouterr().out
        assert custom_name in out
        call_args = mock_post.call_args
        assert call_args[0][1]["name"] == custom_name

    @patch("tools.recipes_cli._api_post")
    @patch("tools.recipes_cli._get_api_key", return_value="fake-key")
    def test_default_scope_is_edit(self, mock_key, mock_post, capsys):
        mock_post.return_value = MOCK_API_RESPONSE
        cmd_share(_make_args())
        call_args = mock_post.call_args
        assert call_args[0][1]["scope"] == "edit"

    @patch("tools.recipes_cli._api_post")
    @patch("tools.recipes_cli._get_api_key", return_value="fake-key")
    def test_output_contains_revoke_hint(self, mock_key, mock_post, capsys):
        mock_post.return_value = MOCK_API_RESPONSE
        cmd_share(_make_args())
        out = capsys.readouterr().out
        assert "DELETE" in out
        assert MOCK_TOKEN_ID in out
