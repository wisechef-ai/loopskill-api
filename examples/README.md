# Examples

Runnable examples for the **Recipes by WiseChef** API at `https://recipes.wisechef.ai`.

## Prerequisites

```bash
export RECIPES_API_KEY=rec_xx...x    # required for all REST + MCP examples
```

Get a key from [recipes.wisechef.ai](https://recipes.wisechef.ai) → Settings → API Keys.

---

## REST examples (`examples/rest/`)

| File | Description |
|------|-------------|
| [`01-list-public-skills.py`](rest/01-list-public-skills.py) | `GET /api/skills/search` — search the public catalog, print slug / tier / description |
| [`02-install-signed-url-roundtrip.py`](rest/02-install-signed-url-roundtrip.py) | `GET /api/skills/install?slug=<slug>` — fetch signed download URL, stream tarball, verify SHA-256, note Ed25519 verification |
| [`03-publish-skill.py`](rest/03-publish-skill.py) | `POST /api/skills/_publish` — package a SKILL.md into a tarball, sign it, submit for review (dry-run by default) |
| [`04-stripe-checkout.py`](rest/04-stripe-checkout.py) | `POST /api/checkout/{tier}` — create a Stripe Checkout Session (illustrative; requires JWT cookie from OAuth) |

### How to run

```bash
# 1. List all skills
RECIPES_API_KEY=*** python examples/rest/01-list-public-skills.py

# 2. Search by keyword
RECIPES_API_KEY=*** python examples/rest/01-list-public-skills.py --query scraping

# 3. Filter by category
RECIPES_API_KEY=*** python examples/rest/01-list-public-skills.py --category marketing

# 4. Install roundtrip for a skill slug
RECIPES_API_KEY=*** python examples/rest/02-install-signed-url-roundtrip.py web-scraper

# 5. Save the tarball locally
RECIPES_API_KEY=*** python examples/rest/02-install-signed-url-roundtrip.py web-scraper --save

# 6. Publish flow (dry-run, no HTTP request sent)
RECIPES_API_KEY=*** python examples/rest/03-publish-skill.py --skill-md path/to/SKILL.md

# 7. Actually publish (requires RECIPES_API_KEY_PRIV for signing)
RECIPES_API_KEY=*** RECIPES_API_KEY_PRIV=~/.keys/ed25519.pem \
    python examples/rest/03-publish-skill.py --skill-md SKILL.md --real

# 8. Checkout session shape (illustrative)
python examples/rest/04-stripe-checkout.py --tier pro
```

---

## MCP examples (`examples/mcp/`)

| File | Description |
|------|-------------|
| [`01-hermes-config-snippet.yaml`](mcp/01-hermes-config-snippet.yaml) | Drop-in `mcpServers` block for `hermes.yaml`; StreamableHTTP via `x-api-key` |
| [`02-claude-desktop-config.json`](mcp/02-claude-desktop-config.json) | Drop-in `mcpServers` block for Claude Desktop `claude_desktop_config.json`; stdio transport |
| [`03-cookbook-share.sh`](mcp/03-cookbook-share.sh) | `curl`-based share-token roundtrip: create → list → rotate → revoke |

### Hermes (StreamableHTTP)

1. Copy the `mcpServers` block from [`01-hermes-config-snippet.yaml`](mcp/01-hermes-config-snippet.yaml) into `~/.hermes/hermes.yaml`.
2. `export MCP_RECIPES_API_KEY=rec_xx...x`
3. Restart Hermes — the `recipes_*` tools appear automatically.

> **Note:** The trailing slash in `https://recipes.wisechef.ai/api/mcp/http/` is required.

### Claude Desktop (stdio)

1. Merge the block from [`02-claude-desktop-config.json`](mcp/02-claude-desktop-config.json) into your Claude Desktop config file.
2. Replace `<your-key>` with your `rec_*` API key.
3. Restart Claude Desktop.

Config file locations:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

### Cookbook share tokens

```bash
RECIPES_API_KEY=*** COOKBOOK_ID=<uuid> bash examples/mcp/03-cookbook-share.sh
```

---

## Authentication

All REST calls use the `x-api-key` header — **not** `Authorization: Bearer`.

```
x-api-key: rec_xxxxxxxxxxxxxxxx
```

Environment variable names:
- `RECIPES_API_KEY` — direct HTTP / stdio usage
- `MCP_RECIPES_API_KEY` — Hermes wizard auto-configuration

Both hold the same `rec_*` key.

---

## Tier names

`free` · `pro` · `pro_plus`

Legacy aliases (`cook` → `pro`, `operator` → `pro_plus`) are accepted until **2026-06-10**.

---

## CI smoke test

The workflow `.github/workflows/examples-smoke.yml` runs `01-list-public-skills.py`
against the live API on every PR. If `RECIPES_API_KEY` is unavailable it exits 0
with a notice (does not fail the build).
