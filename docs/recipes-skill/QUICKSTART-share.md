# Quickstart: Share a Cookbook with Another Agent in 3 Minutes

Cookbook share tokens let you grant another agent access to your private cookbook — without sharing API keys or creating accounts. Perfect for team workflows, client handoffs, and multi-agent setups.

## Prerequisites

- The Recipes CLI: `tools/recipes_cli.py` from the [recipes-skill repo](https://github.com/wisechef-ai/recipes-skill)
- An API key (set via `RECIPES_API_KEY` env var or stored in `~/.hermes/secrets/`)
- A cookbook you want to share

## Step 1: Create a Share Token

```bash
python3 tools/recipes_cli.py share YOUR_COOKBOOK_ID --name "Shared with teammate"
```

Output:

```
✓ Share token created
  Token:   cbt_a1b2c3d4_e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0
  Prefix:  cbt_a1b2c3d4
  Scope:   edit
  Name:    Shared with teammate
  Expires: never (revoke with DELETE /api/cookbooks/YOUR_COOKBOOK_ID/share-tokens/TOKEN_ID)

============================================================
Copy-paste the block that matches your client:
============================================================

# ── Hermes config.yaml ──
mcpServers:
  recipes-shared:
    transport: streamable-http
    url: https://recipes.wisechef.ai/api/mcp/http
    headers:
      x-api-key: cbt_a1b2c3d4_e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0

// ── Claude Desktop  (claude_desktop_config.json) ──
{
  "mcpServers": {
    "recipes-shared": {
      "type": "streamable-http",
      "url": "https://recipes.wisechef.ai/api/mcp/http",
      "headers": {
        "x-api-key": "cbt_a1b2c3d4_e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
      }
    }
  }
}
```

## Step 2: Send the Config Block

Copy the relevant config block and send it to your teammate. They paste it into their agent's config file and restart.

That's it — their agent now has full MCP access to your cookbook.

## Read-Only Access

Want to share without giving edit permissions?

```bash
python3 tools/recipes_cli.py share YOUR_COOKBOOK_ID --read-only --name "View-only access"
```

This sets `scope=read` — the other agent can search and recall skills but can't modify the cookbook.

## Revoke Access

When you're done sharing, revoke the token:

```bash
python3 tools/recipes_cli.py revoke YOUR_COOKBOOK_ID TOKEN_ID
```

Access is revoked instantly.

## Security Notes

- Share tokens are long-lived (no expiry) but revocable at any time
- Each token is unique — revoke one without affecting others
- Tokens follow the format `cbt_<8hex>_<32hex>` for easy identification
- The token serves as the API key in the MCP config — no separate auth needed

---

**Three minutes, zero friction.** Share cookbooks like you share links. 🔗
