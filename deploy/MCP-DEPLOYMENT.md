# MCP deployment — split-process pattern

## Why this matters

The MCP `StreamableHTTPSessionManager` keeps session state **in-process only**.
There is no shared store, no Redis backend, no cross-process gossip.

`recipes-api` runs `uvicorn --workers 2` on `:3360` to handle real API traffic.
Caddy's `reverse_proxy` distributes incoming requests randomly across the two
worker sockets. That's fine for stateless REST. It is **fatal** for MCP:

1. Client POSTs `initialize` → lands on worker A → session ID created in A's memory.
2. SDK opens a server-initiated GET stream on the same session ID → lands on worker B.
3. Worker B has no record of the session → returns HTTP 404 with
   `{"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,"message":"Session not found"}}`.
4. The SDK treats that as session invalidation. All subsequent POSTs return
   `Session terminated`. Client tooling reports `MCP: registered 0 tool(s)`.

The user-visible symptom: every native MCP client (Hermes, Claude Desktop,
mac-side Hermes, anything using the Python SDK ≥ 1.4) fails to register the
recipes server even though manual POST-only `curl` works (because curl reuses
the same TCP connection long enough to hit one worker).

## The fix: dedicated single-worker MCP process

- `wiserecipes-api.service` keeps `--workers 2` on `127.0.0.1:3360` for the REST API.
- `wiserecipes-mcp.service` runs `--workers 1` on `127.0.0.1:3362` and serves the
  same FastAPI `app.main:app` — the StreamableHTTP mount, the SSE mount, and the
  `/api/mcp/healthz` route are inside that ASGI app.
- Caddy is configured so `/api/mcp/*` and `/api/mcp` route to `:3362`. Every other
  `/api/*` path continues to `:3360`. The MCP block **must** come before the
  catch-all `handle /api/*` block; Caddy matches in source order.

## Files

- `deploy/wiserecipes-mcp.service` — install at `/etc/systemd/system/wiserecipes-mcp.service`
- `deploy/caddy-mcp.snippet` — paste into the `recipes.wisechef.ai` site block,
  immediately after the `/openapi.json` handle.

## Install

```
sudo cp deploy/wiserecipes-mcp.service /etc/systemd/system/wiserecipes-mcp.service
sudo systemctl daemon-reload
sudo systemctl enable --now wiserecipes-mcp.service

# Edit /etc/caddy/Caddyfile, paste the MCP handle block before `handle /api/*`.
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

## Verify

```
# 1. Healthz on the new port
curl -s http://127.0.0.1:3362/api/mcp/healthz | jq .

# 2. End-to-end SDK handshake (replace $KEY with a valid x-api-key)
curl -s -X POST https://recipes.wisechef.ai/api/mcp/http/ \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H "x-api-key: $KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' \
  -D /tmp/H.txt -o /dev/null
SID=$(grep -i mcp-session-id /tmp/H.txt | awk '{print $2}' | tr -d '\r\n')

# notifications/initialized → 202
curl -s -X POST https://recipes.wisechef.ai/api/mcp/http/ \
  -H "x-api-key: $KEY" -H "Mcp-Session-Id: $SID" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' -w 'HTTP %{http_code}\n'

# GET stream — must NOT 404 and must NOT kill the session
timeout 3 curl -s -X GET https://recipes.wisechef.ai/api/mcp/http/ \
  -H 'Accept: text/event-stream' \
  -H "x-api-key: $KEY" -H "Mcp-Session-Id: $SID" \
  -w 'HTTP %{http_code}\n' || true

# tools/list — must return all tools on the same session
curl -s -X POST https://recipes.wisechef.ai/api/mcp/http/ \
  -H "x-api-key: $KEY" -H "Mcp-Session-Id: $SID" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
```

From a Hermes host:

```
hermes mcp test recipes
# Expected: ✓ Connected, ✓ Tools discovered: 14
```

## Why not a shared session store?

The MCP Python SDK 1.4 does not expose a pluggable session-state backend. A
Redis-backed `StreamableHTTPSessionManager` would require either a fork or a
wrapper that intercepts ASGI scope and forwards messages across workers — both
are significantly more code than running the MCP path on a dedicated worker,
with no observable upside until we exceed the per-process throughput of one
uvicorn worker (which, given MCP traffic is interactive and low-volume, is far
off).

If/when the SDK adds a `session_store` parameter, this file should be updated
and the split removed.

## Pitfalls

- **Order matters in Caddyfile.** Putting `handle /api/mcp/*` after `handle
  /api/*` is silent breakage — the catch-all wins.
- **Don't bump `--workers` above 1 on `wiserecipes-mcp.service`** without first
  implementing a shared session store. Symptoms: native MCP clients can't list
  tools; manual curl works.
- **`flush_interval -1`** on the Caddy reverse_proxy is required so SSE events
  are not buffered. Without it the GET stream appears to hang.
- **The healthz endpoint at `/api/mcp/healthz`** is served by both processes
  (same code), so monitor the public URL — that traffic now reaches `:3362`.
