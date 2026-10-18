# loopskill-mcp

Thin stdio MCP bridge for [LoopSkill](https://app.loopskill.io) — the skill,
loop and bundle marketplace for AI agents.

`loopskill-mcp` is a **local stdio MCP server** that any MCP client (Claude
Desktop, Hermes, Codex CLI, Cursor, ...) can spawn directly. Internally it
opens one persistent MCP client session to the **hosted** StreamableHTTP
endpoint at `https://app.loopskill.io/api/mcp/http/` (the same server
`app/mcp/server.py` in this repo builds) and proxies every `tools/list` /
`tools/call` through it. You get the full live tool catalog (46 tools,
including `loopskill_search`, `loopskill_install`, `loopskill_bundle_install`,
...) without running a database or the full FastAPI app locally.

## Install & run

```bash
uvx loopskill-mcp
```

That's it — `uvx` fetches the package from PyPI, creates an ephemeral venv,
and runs the `loopskill-mcp` console script, which speaks MCP over stdio.

For local testing against an unpublished build:

```bash
python -m build packaging/loopskill-mcp
uvx --from ./packaging/loopskill-mcp/dist/loopskill_mcp-*.whl loopskill-mcp
```

## Environment variables

Mirrors the promise in [`docs/SELF_HOST.md`](../../docs/SELF_HOST.md)
("Environment variables" section):

| Variable | When to use |
|----------|-------------|
| `LOOPSKILL_API_KEY` | Canonical — the standard key for any MCP client. **Required to call tools**: the hosted endpoint answers `401` to every MCP request without a valid `x-api-key`. Without one the bridge still starts and lists all tools (from the public server card), but each `tools/call` returns an error telling you how to get a free key — see "Getting an API key" below. |
| `MCP_LOOPSKILL_API_KEY` | Same key, alternate name used by the Hermes wizard integration. |
| `RECIPES_API_KEY` / `MCP_RECIPES_API_KEY` | Legacy names, still accepted for existing integrations. |
| `LOOPSKILL_MCP_URL` | Escape hatch to point at a self-hosted LoopSkill instance (default: `https://app.loopskill.io/api/mcp/http/`). Not part of the public promise; for `docs/SELF_HOST.md` deployments. |
| `LOOPSKILL_SERVER_CARD_URL` | Override for the keyless tool catalogue (default: `<origin of LOOPSKILL_MCP_URL>/.well-known/mcp/server-card.json`). |

Precedence when more than one is set: `LOOPSKILL_API_KEY` >
`MCP_LOOPSKILL_API_KEY` > `RECIPES_API_KEY` > `MCP_RECIPES_API_KEY`.

## Getting an API key

The hosted MCP server itself is **not** anonymous: it answers `401 Invalid or
missing x-api-key header` to keyless requests. Without a key the bridge still
completes `initialize` and answers `tools/list` from the public, keyless server
card (`/.well-known/mcp/server-card.json`, the same file MCP directories read),
so you can see every tool and its schema first. Each keyless `tools/call`
returns `isError: true` with the steps below. With a key configured, failures
are reported as-is and are never replaced by the card. Keys are free and start
with `rec_`:

- **Humans:** sign in at <https://app.loopskill.io/signin>, then create a key at
  <https://app.loopskill.io/library>.
- **Agents (no signup):** self-enrol with `POST https://app.loopskill.io/api/agents/register`
  — you prove possession of an Ed25519 key; the response returns a `rec_agent_…`
  key once. The signed-string format is versioned; read it from
  <https://app.loopskill.io/.well-known/agent.json> (`registration`). Agent keys
  can search, install free skills and compose bundles, but not check out, bill
  or run sandboxes. Registration is rate limited per IP and per day.

Only some plain REST endpoints are keyless (for example public catalog search
at `/api/skills/search` and free-skill install over REST); the MCP endpoint is
not.

## Client config snippets

### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "loopskill": {
      "command": "uvx",
      "args": ["loopskill-mcp"],
      "env": {
        "LOOPSKILL_API_KEY": "<your-key>"
      }
    }
  }
}
```

### Hermes (`~/.hermes/config.yaml`)

```yaml
mcpServers:
  loopskill:
    command: uvx
    args: ["loopskill-mcp"]
    env:
      LOOPSKILL_API_KEY: "<your-key>"
```

(Hermes can also connect directly over StreamableHTTP without this bridge —
see the main repo README's "Hermes (StreamableHTTP)" section. Use this
stdio bridge for MCP clients that only support the stdio/local-process
transport shape.)

## What this package is *not*

It is not the LoopSkill server itself. `app/mcp/server.py` in this repo is
the real MCP server (StreamableHTTP + SSE + stdio), and it requires the
full FastAPI app and a database — it is not meant to run on an end user's
machine. This package exists so `uvx loopskill-mcp` — documented across
this repo's docs as the install path for local/stdio MCP clients — is a
real, runnable artifact instead of a 404.

## Development

```bash
cd packaging/loopskill-mcp
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q                                   # unit tests, no network
LOOPSKILL_MCP_LIVE_TEST=1 \
LOOPSKILL_MCP_LIVE_TEST_KEY=<optional-key> \
  pytest -q -m network tests/test_live_smoke.py -v   # live handshake smoke test
python -m build
twine check dist/*
```

## Publish runbook (human-gated — not run by CI or agents)

```bash
cd packaging/loopskill-mcp
rm -rf dist build src/*.egg-info
python -m build
twine check dist/*
twine upload dist/*          # prompts for a PyPI API token
```
