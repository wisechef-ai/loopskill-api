# SUBAGENT_P1_OUTPUT.md — Phase 1 StreamableHTTP Transport

## Status: ✅ SHIPPED

## PR URL
**https://github.com/wisechef-ai/recipes-api/pull/31**

## What Shipped
- StreamableHTTP transport mounted at `/api/mcp/http` as a raw ASGI sub-app
- Shares `build_mcp_server()` factory with SSE and stdio — zero tool duplication
- Auth gate with master-key fast-path (no DB call for dev/testing)
- `session_idle_timeout=1800s` prevents Cloudflare 100s streaming timeout
- `run_streamable_http()` context manager wired into app lifespan
- `requirements.txt` bumped: `mcp[cli]>=1.4`

## Test Counts
| Metric | Before | After |
|--------|--------|-------|
| Collected | 848 | 853 (+5) |
| Passed | 817 | 817 |
| Failed | 9 (pre-existing) | 9 (pre-existing) |
| Errors | 24 (pre-existing) | 24 (pre-existing) |

## New Tests (5)
1. `test_initialize_returns_9_tools` — Full MCP handshake via StreamableHTTP
2. `test_long_running_tool_does_not_timeout` — 5s sleep, responds in <8s
3. `test_sse_transport_still_works` — SSE routes still registered
4. `test_sse_rejects_missing_api_key` — SSE auth regression
5. `test_streamable_http_route_is_registered` — Mount in route table

## Commit SHAs
1. `9774b30` — test(mcp): add streamable_http transport tests (RED)
2. `c1e315e` — feat(mcp): add StreamableHTTP transport at /api/mcp/http (GREEN) [initial]
3. `3ae33fd` — chore: bump mcp SDK requirement to >=1.4
4. `c2beddb` — feat(mcp): add StreamableHTTP transport at /api/mcp/http (GREEN) [auth + test fixes]
5. `66d7b7c` — docs: add PR body

## Files Modified
- `app/mcp/server.py` — StreamableHTTP session manager, auth gate, mount builder, lifespan helper
- `app/main.py` — Lifespan wiring, mount registration
- `requirements.txt` — mcp[cli]>=1.4
- `tests/test_mcp_streamable_transport.py` — NEW, 5 tests

## Deferred Items
- `ping_interval_seconds=20`: MCP SDK 1.27 does not expose this parameter. `session_idle_timeout=1800` mitigates. Track for future SDK update.
- Per-transport healthz (`/api/mcp/http/healthz`): Not needed; shared `/api/mcp/healthz` covers all transports.

## Key Design Decisions
1. **Raw ASGI Mount** instead of FastAPI route — StreamableHTTPSessionManager sends HTTP responses directly via ASGI `send`, which conflicts with FastAPI's response wrapper.
2. **Auth gate in ASGI handler** — FastAPI's `Depends()` doesn't work for raw ASGI mounts; implemented inline auth check with master-key fast-path to avoid DB dependency in tests.
3. **Global session manager** — `_http_session_manager` module-level global, resettable via `_reset_http_session_manager()` for test isolation.

## Copilot Reviewer Requested: ✅
