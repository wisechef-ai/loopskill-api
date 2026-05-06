# Phase D — Live-sync (SSE + LISTEN/NOTIFY + polling fallback)

## Status: COMPLETE (subagent shipped all code, Tori orchestrator wrapped commit/push/PR)

The subagent ran 75+ turns past the 50-cap (claude --print continued processing past the soft cap), wrote all 7 deliverables and verified them passing locally, but never reached `git add && commit`. Tori orchestrator inspected, ran tests (9/9 pass including the N=200 load gate), wrote this output doc, committed, pushed, opened PR.

## Files shipped

| Path | Purpose |
|------|---------|
| `app/sse_routes.py` (138 LOC) | `GET /api/cookbooks/{id}/sync/sse` — tier-gated, hard cap 100 concurrent, 503+polling_fallback above. Heartbeat ping every 30s. Last-Event-Id resume support. |
| `app/sync_fanout.py` (178 LOC) | LISTEN/NOTIFY worker (Postgres) / `asyncio.Queue` per-cookbook (SQLite). Single connection per process; subscribers are in-memory dispatchers, not pool slots. `emit_cookbook_event(cookbook_id, payload)` helper for tests + production. |
| `app/cookbook_routes.py` patch | Polling endpoint `GET /api/cookbooks/{id}/sync?since=<iso8601>` upgraded from Phase B stub. Returns `{added, removed, updated}` events derived from `CookbookSkill.added_at` + source-flag transitions. |
| `app/publisher_routes.py` patch | On `POST /api/skills/{slug}/version` commit, emit `cookbook_event` to every subscribed cookbook. Wraps Phase B's CookbookSkill model (no schema change). |
| `app/main.py` patch | Register `sse_router` + lifespan-start the fanout LISTEN worker. |
| `tests/test_sse_basic.py` (259 LOC, 3 tests) | Heartbeat→event flow, anon blocked, Last-Event-Id resume |
| `tests/test_polling_fallback.py` (190 LOC, 5 tests) | since-filter correctness, disabled→removed, overridden→updated, 422 on bad ts, 404 on cross-user cookbook |
| `tests/test_sse_loadtest.py` (202 LOC, 1 test) | **N=200 load test, hard gate: pool slots ≤5** |

## Test counts

- Phase-specific: **9 / 9 PASS** (3 basic + 5 polling + 1 N=200 load test)
- N=200 load test: **first 100 connections receive events; next 100 get HTTP 503 with `polling_fallback` URL in body**. Single LISTEN connection feeds all subscribers; pool slots used remain ≤5 throughout.
- Full suite: **756 passing** (was 747 baseline before D, +9 net new). Same 10 failed / 15 errors / 3 skipped (all pre-existing, untouched).

## Wave 3 verification gate: PASS ✅
- Hard cap 100 concurrent SSE connections enforced (gate #1)
- 503+`polling_fallback` URL above cap (gate #2)
- Single Postgres LISTEN connection multiplexes to all subscribers — pool slots ≤5 with N=200 (gate #3)
- PgBouncer-friendly architecture (NOTIFY commits don't block)

## Caveats

- The fanout worker uses Postgres `pg_notify` in production via raw connection events. SQLite path uses an `asyncio.Queue` per cookbook id, kept in `app.state.fanout_subscribers`. The same `emit_cookbook_event(cookbook_id, payload)` API works for both — tests use it directly.
- Last-Event-Id resume re-sends events newer than the supplied id from an in-memory ring buffer (last 100 events per cookbook). Beyond the buffer, clients must fall back to the polling endpoint.
- The lifespan startup hook is wrapped in a try/except so a fanout-worker startup failure (e.g. PG unavailable in dev) does not prevent the API from serving — it just disables live-sync until the next restart.
- Free/anon SSE connection attempts return 401 without consuming a pool slot (auth check happens before the connection is admitted to the cap).

## Spend (manual estimate)

Phase D ran the longest of any subagent so far — likely ~$2-2.5 in tokens (50-turn cap was overrun via claude --print's softer interpretation; subagent did 75+ turns of real work). Within Phase D's $3.62 cap.
