#!/usr/bin/env python3
"""Recipes CLI — share cookbooks with any MCP-compatible agent.

Usage:
    python3 tools/recipes_cli.py share <cookbook_id> [--read-only] [--name LABEL]

Reads RECIPES_API_KEY from env, or falls back to
~/.hermes/secrets/tori-recipes-mcp-key.txt for local dev.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

API_BASE = "https://recipes.wisechef.ai"
SECRET_FALLBACK = Path.home() / ".hermes" / "secrets" / "tori-recipes-mcp-key.txt"
USER_AGENT = "recipes-cli/1.0 (+https://github.com/wisechef-ai/recipes-skill)"


def _get_api_key() -> str:
    key = os.environ.get("RECIPES_API_KEY", "").strip()
    if key:
        return key
    if SECRET_FALLBACK.exists():
        return SECRET_FALLBACK.read_text().strip()
    print(
        "Error: RECIPES_API_KEY not set and "
        f"{SECRET_FALLBACK} not found.\n"
        "Set the env var or create the fallback file.",
        file=sys.stderr,
    )
    sys.exit(1)


def _api_post(path: str, body: dict, api_key: str) -> dict:
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode()
    req = Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-api-key", api_key)
    # CF Bot Fight Mode blocks default urllib UA. Send a real one.
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urlopen(req) as resp:
            return json.loads(resp.read())
    except HTTPError as exc:
        detail = exc.read().decode()
        print(f"API error {exc.code}: {detail}", file=sys.stderr)
        sys.exit(1)


def _api_delete(path: str, api_key: str) -> None:
    url = f"{API_BASE}{path}"
    req = Request(url, method="DELETE")
    req.add_header("x-api-key", api_key)
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urlopen(req) as resp:
            pass  # 204 / 200 expected
    except HTTPError as exc:
        detail = exc.read().decode()
        print(f"API error {exc.code}: {detail}", file=sys.stderr)
        sys.exit(1)


def _print_config_blocks(token: str) -> None:
    """Print copy-paste MCP config blocks for Hermes and Claude Desktop."""
    url = f"{API_BASE}/api/mcp/http"
    divider = "=" * 60

    hermes_block = f"""\
# ── Hermes config.yaml ──
mcpServers:
  recipes-shared:
    transport: streamable-http
    url: {url}
    headers:
      x-api-key: {token}
"""

    claude_block = f"""\
// ── Claude Desktop  (claude_desktop_config.json) ──
{{
  "mcpServers": {{
    "recipes-shared": {{
      "type": "streamable-http",
      "url": "{url}",
      "headers": {{
        "x-api-key": "{token}"
      }}
    }}
  }}
}}
"""

    print(divider)
    print("Copy-paste the block that matches your client:")
    print(divider)
    print()
    print(hermes_block)
    print()
    print(claude_block)
    print(divider)


def cmd_share(args: argparse.Namespace) -> None:
    api_key = _get_api_key()
    scope = "read" if args.read_only else "edit"
    name = args.name or "shared via CLI"

    resp = _api_post(
        f"/api/cookbooks/{args.cookbook_id}/share-tokens",
        {"name": name, "scope": scope},
        api_key,
    )

    token = resp.get("token", "")
    print(f"✓ Share token created")
    print(f"  Token:   {token}")
    print(f"  Prefix:  {resp.get('prefix', '')}")
    print(f"  Scope:   {resp.get('scope', scope)}")
    print(f"  Name:    {resp.get('name', name)}")
    print(f"  Expires: never (revoke with DELETE /api/cookbooks/{args.cookbook_id}/share-tokens/{resp.get('id', '')})")
    print()
    _print_config_blocks(token)


def cmd_revoke(args: argparse.Namespace) -> None:
    api_key = _get_api_key()
    _api_delete(
        f"/api/cookbooks/{args.cookbook_id}/share-tokens/{args.token_id}",
        api_key,
    )
    print(f"✓ Token {args.token_id} revoked.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="recipes",
        description="Recipes CLI — share cookbooks with any MCP-compatible agent.",
    )
    sub = parser.add_subparsers(dest="command")

    # share
    share_p = sub.add_parser("share", help="Create a share token for a cookbook")
    share_p.add_argument("cookbook_id", help="UUID of the cookbook to share")
    share_p.add_argument(
        "--read-only",
        action="store_true",
        dest="read_only",
        help="Grant read-only (scope=read) instead of edit",
    )
    share_p.add_argument("--name", default="shared via CLI", help="Label for this token")

    # revoke
    revoke_p = sub.add_parser("revoke", help="Revoke a share token")
    revoke_p.add_argument("cookbook_id", help="UUID of the cookbook")
    revoke_p.add_argument("token_id", help="ID of the share token to revoke")

    args = parser.parse_args()
    if args.command == "share":
        cmd_share(args)
    elif args.command == "revoke":
        cmd_revoke(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
