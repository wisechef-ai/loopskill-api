# V7 Phase A — Recipes-MCP Server (subagent A output)

## Summary

Phase A delivers the foundational `recipes-mcp` server: an MCP-compliant tool
surface that lets agents discover, install, and audit Recipes skills via the
official MCP wire protocol. Two transports are wired:

- **stdio** — `python -m app.mcp` for Claude Desktop and other local clients.
- **SSE/HTTP** — mounted on the FastAPI app at `/mcp/sse` (event-stream) and
  `/mcp/messages/` (JSON-RPC POSTs). Auth reuses the existing `x-api-key`
  validator (`app.middleware`) so the same operator key works across REST
  and MCP.

Eight tools ship in this phase. Four are full implementations; four are
deliberate stubs that future phases (C, E, G) replace.

## Files added

| Path | Purpose |
|---|---|
| `app/mcp/__init__.py` | Package marker — re-exports the `router` and `build_mcp_server`. |
| `app/mcp/__main__.py` | stdio entry point (`python -m app.mcp`). |
| `app/mcp/server.py` | MCP `Server` factory + FastAPI router with SSE transport, tool dispatch, and stdio runner. |
| `app/mcp/auth.py` | `validate_key()` — operator scope today; raises `NotImplementedError("phase-C")` for `sub_*` keys. |
| `app/mcp/tools/__init__.py` | Re-exports the eight tool callables. |
| `app/mcp/tools/search.py` | `recipes_search` — public-skill catalog search. |
| `app/mcp/tools/install.py` | `recipes_install` — signed tarball URL + manifest, writes `InstallEvent`. |
| `app/mcp/tools/list_cookbook.py` | `recipes_list_cookbook` — read-path against `Cookbook` / `CookbookSkill`. |
| `app/mcp/tools/recall.py` | Phase E stub. |
| `app/mcp/tools/recipify.py` | Phase G stub. |
| `app/mcp/tools/carousel_today.py` | `recipes_carousel_today` — proxy for today's curated carousel. |
| `app/mcp/tools/subrecipe_resolve.py` | Phase C stub — always returns `{"scope": "operator"}`. |
| `app/mcp/tools/doctor.py` | `recipes_doctor` — local install audit (SKILL.md, `_meta.json`, hardcoded `/home/<user>/` paths). |
| `tests/test_mcp_tools.py` | 14 unit tests, ≥1 per tool. |
| `tests/test_mcp_server.py` | 6 integration tests covering router wiring, auth gates, and in-process tool dispatch. |
| `SPRINT_DOCS/V7_SUBAGENT_A_OUTPUT.md` | This file. |

## Files patched

- `app/main.py` — added `from app.mcp.server import router as mcp_router` and
  `app.include_router(mcp_router)` (one line each, no other behaviour changes).

## Test counts

| Suite | Result |
|---|---|
| `tests/test_mcp_tools.py` | **14 / 14 passed** |
| `tests/test_mcp_server.py` | **6 / 6 passed** |
| Full suite (with the same `--ignore` set as baseline) | **704 passed**, 10 failed, 15 errors, 3 skipped |
| Baseline at branch-cut | 684 passed, 10 failed, 15 errors, 3 skipped |

→ **+20 new tests, zero regressions.** All pre-existing failures and errors
are unchanged (alembic/migration tests, Stripe/JWT auth, sandbox firejail,
external dependencies — none touched in this phase).

## Auth model

- `x-api-key` header on `/mcp/sse` and `/mcp/messages/`.
- Master key (`settings.API_KEY`) → `{"scope": "operator", "user_id": None}`.
- Real `APIKey` row → `{"scope": "operator", "user_id": <uuid>}`.
- Anything else → 401 from FastAPI `Depends(_authenticate)`.
- `sub_*` keys raise `NotImplementedError("phase-C")` and are routed through
  `recipes_subrecipe_resolve` (which is itself a stub returning `operator`).

## Caveats and follow-ups

1. **No live SSE handshake test.** Driving a full MCP `initialize` round-trip
   over `TestClient.stream` deadlocks — `Server.run()` is a long-lived loop
   that does not unblock when the client closes. The integration test
   instead exercises the same dispatch path in-process via
   `server.request_handlers[CallToolRequest]`. The SDK's own test suite
   covers wire-level correctness.

2. **Sub-recipe keys.** Phase C will replace
   `app/mcp/tools/subrecipe_resolve.py` and the `sub_*` branch in
   `app/mcp/auth.py`. The current stubs are deliberately discoverable so
   integration tests for Phase C can flip them on without churning callers.

3. **Cookbook write-path.** Phase A only ships a read tool
   (`recipes_list_cookbook`). The full cookbook CRUD endpoints belong to
   Phase B; we intentionally do **not** export a write surface yet.

4. **AgentPact source not used.** `~/agentpact/` was empty on the build
   host. Per the premortem fallback, we built directly against the official
   `mcp` Python SDK (already pip-installed). All MCP type usage is via the
   public `mcp.types` / `mcp.server.lowlevel` / `mcp.server.sse` API.

5. **`InstallEvent` write on every install call.** Mirrors the existing REST
   handler's behaviour — keeps install-count analytics consistent across
   transports. If MCP install volume becomes noisy, we can add a `dry_run`
   flag downstream.
