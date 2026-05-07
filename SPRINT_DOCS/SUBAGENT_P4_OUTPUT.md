# Phase 4 Output — BM25 Auto-Reindex on Publish/Archive

**Branch**: `v7.1/phase-4-bm25-reindex`
**PR**: https://github.com/wisechef-ai/recipes-api/pull/30
**Date**: 2026-05-07

## Summary

Implemented Phase 4 of the v7.1 sprint: automatic BM25 search index maintenance via pure Postgres `tsvector`. **No embeddings added.** Embeddings deferred to v7.2 per Adam directive 2026-05-07.

## Performance

```
[P4-PERF] publish_201_response_time = 184.2ms
```

Well under the 500ms p95 threshold. BM25 is synchronous — `to_tsvector` is <10ms in Postgres.

## Test Results

- **Baseline**: 812 passed, 9 failed (pre-existing), 24 errors (pre-existing)
- **Phase 4**: 817 passed (812 + 5 new), 9 failed (unchanged), 24 errors (unchanged)
- **Zero new failures.**

### New Tests (tests/test_reindex.py)

| Test | Status |
|------|--------|
| `test_publish_updates_search_within_one_second` | ✅ PASS |
| `test_archive_drops_from_search` | ✅ PASS |
| `test_admin_reindex_all_no_regression` | ✅ PASS |
| `test_publish_201_response_under_500ms` | ✅ PASS (184.2ms) |
| `test_search_vector_is_set_after_publish` | ✅ PASS |

## Files Changed

| File | Change |
|------|--------|
| `app/models.py` | Added `search_vector` (Text) and `is_archived` (Boolean) to Skill model |
| `app/search_index.py` | **NEW** — `reindex_bm25(slug, db, archive=False)` with Postgres tsvector + SQLite fallback; `reindex_all(db)` for catastrophic recovery |
| `app/publisher_routes.py` | Hook `reindex_bm25()` after skill_versions commit; added `POST /{slug}/_archive` endpoint |
| `app/admin_routes.py` | **NEW** — `POST /api/admin/reindex-all` (master-key only) |
| `app/recall_routes.py` | Filter out archived skills (`is_archived == False`) |
| `app/main.py` | Register `admin_router` |
| `tests/test_reindex.py` | **NEW** — 5 tests covering full Phase 4 spec |

## Dependencies

- **requirements.txt diff from origin/main**: ZERO CHANGES
- No embedding libraries added. Phase 4 is BM25-ONLY.

## Commits

1. `4848eca` — RED: Phase 4 BM25 reindex tests (4 failing, 1 passing)
2. `ece48f5` — GREEN: Phase 4 BM25 reindex — all 5 tests passing

## Notes

- SQLite tests use a plain-text fallback for `to_tsvector` (function doesn't exist in SQLite)
- The `search_vector` column is `Text` type for cross-database compatibility; in production Postgres the `to_tsvector()` function populates it with proper tsvector output
- The `/api/recall` endpoint now excludes archived skills via `is_archived == False` filter
- Archive route is master-key only (admin access required)
