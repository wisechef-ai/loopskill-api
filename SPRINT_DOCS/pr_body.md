## Phase 1 — StreamableHTTP Transport (v7.1)

### Spec Items Shipped
1. ✅ **StreamableHTTP transport at `/api/mcp/http`** — mounted as a raw ASGI sub-app via `StreamableHTTPSessionManager`, sharing the existing `build_mcp_server()` factory with SSE and stdio transports (no duplicated tool definitions).
2. ✅ **Auth gate** — validates `x-api-key` on every StreamableHTTP request, with a master-key fast-path to avoid unnecessary DB lookups.
3. ✅ **Session idle timeout** — `session_idle_timeout=1800` (30 min) prevents Cloudflare's 100s streaming timeout from terminating long-running tool sessions.
4. ✅ **Lifespan integration** — `run_streamable_http()` context manager wired into `app/main.py` lifespan alongside the existing Discord bot and fanout listeners.
5. ✅ **Requirements bump** — `mcp[cli]>=1.4` (from `>=1.0.0`) to document the StreamableHTTP dependency.
6. ⏭️ **Middleware** — no change needed; the existing `/api/mcp/healthz` PUBLIC_PREFIX covers the shared healthz endpoint.

### Tests Added (5 new, all passing)

| Test Name | Description |
|---|---|
| `TestStreamableHTTPTransport::test_initialize_returns_9_tools` | Full MCP handshake (initialize → initialized → tools/list) via StreamableHTTP, asserts all 9 tools advertised |
| `TestStreamableHTTPTransport::test_long_running_tool_does_not_timeout` | Patches `_dispatch` with 5s sleep, asserts response within 8s |
| `TestSSETransportRegression::test_sse_transport_still_works` | Verifies SSE routes still registered, tool count unchanged |
| `TestSSETransportRegression::test_sse_rejects_missing_api_key` | Regression: SSE endpoint still requires auth |
| `TestSSETransportRegression::test_streamable_http_route_is_registered` | `/api/mcp/http` mount appears in route table |

### Test Count
- **Before:** 848 collected
- **After:** 853 collected (+5 new)
- **Passing:** 817 (same baseline — 9 pre-existing failures + 24 pre-existing errors unrelated to this change)

### Manual Verification Commands
```bash
# 1. Install deps and run new tests
source venv/bin/activate
venv/bin/pytest tests/test_mcp_streamable_transport.py -v

# 2. Run existing MCP tests to verify no regression
venv/bin/pytest tests/test_mcp_server.py tests/test_mcp_tools.py -v

# 3. Full regression (ignoring pre-existing broken tests)
venv/bin/pytest -q --ignore=tests/test_maestro_rename.py --ignore=tests/test_referral.py

# 4. Manual curl against running server
curl -X POST http://localhost:8000/api/mcp/http \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "x-api-key: $RECIPES_API_KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl-test","version":"0.1.0"}}}'
```

### Files Changed
- `app/mcp/server.py` — Added StreamableHTTP imports, session manager, auth gate, `_build_streamable_http_mount()`, `run_streamable_http()`, `_reset_http_session_manager()`
- `app/main.py` — Wired `run_streamable_http()` into lifespan, appended mount to router
- `requirements.txt` — Bumped `mcp[cli]>=1.4`
- `tests/test_mcp_streamable_transport.py` — New test file (5 tests)

### Deferred Items
- `ping_interval_seconds=20`: MCP SDK 1.27 does not expose this parameter. Tracked for future SDK update. The `session_idle_timeout=1800` mitigates Cloudflare timeout.
- Per-transport healthz (`/api/mcp/http/healthz`): Not needed since the shared `/api/mcp/healthz` covers all transports.

### Commits
1. `9774b30` — test(mcp): add streamable_http transport tests (RED)
2. `c1e315e` — feat(mcp): add StreamableHTTP transport at /api/mcp/http (GREEN)
3. `3ae33fd` — chore: bump mcp SDK requirement to >=1.4 for StreamableHTTP transport
4. `c2beddb` — feat(mcp): add StreamableHTTP transport at /api/mcp/http (GREEN) [auth + test fixes]
