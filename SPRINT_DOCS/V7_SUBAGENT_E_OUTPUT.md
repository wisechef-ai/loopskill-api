# v7 Phase E — Recall endpoint output

## Summary

`/api/recall` (and the unstubbed MCP `recipes_recall` tool) ship a hybrid
vector + BM25 recall ranker over the public skill catalog. Tier-gated,
cookbook-aware, with deterministic CPU-only embeddings.

## Files

| Path | Purpose |
| ---- | ------- |
| `alembic/versions/c5d6e7f8a902_v7_phase_e_pgvector.py` | Adds `skills.embedding`. Postgres → `vector(384)` (with pgvector); SQLite → JSON-encoded floats in TEXT. Down-revision `b3c4d5e6f701`. |
| `app/embeddings.py` | Lazy singleton `BAAI/bge-small-en-v1.5`. `embed_text`, `embed_skill`, `cosine`. Hash-bag fallback when ST is missing — no API calls. |
| `app/ranking.py` | `score_vector`, `score_bm25` (in-process BM25 over title×3 + desc + tags×2), `combine` (0.6·vec + 0.4·sigmoid(bm25), tier-zero, cookbook +5%). |
| `app/recall_routes.py` | `POST /api/recall` (Pydantic `RecallIn`/`RecallHit`/`RecallOut`). Service `recall_skills()` is what the MCP tool also calls. |
| `app/mcp/tools/recall.py` | Replaces Phase A stub. Validates `query`, delegates to service. |
| `app/mcp/server.py` | Tool descriptor updated; recall now declares full input schema. |
| `app/main.py` | One-line router include for `recall_router`. |
| `app/models.py` | New `Skill.embedding` Text column (kept Text for cross-DB safety; pgvector reads/writes still work). |
| `scripts/backfill_embeddings.py` | Idempotent backfill CLI: `--force` re-embeds everything; otherwise only nulls. Postgres uses `vector` literal; SQLite uses JSON. |
| `tests/test_recall.py` | 12 tests. Unit + tier-gating + 50-query held-out eval set. |
| `tests/test_recall_mcp.py` | 3 tests. Round-trip via `call_tool_sync` + empty-query error path. |
| `tests/test_mcp_tools.py` | Updated stub assertion → new contract. |

## Eval set

* 25-skill seeded catalog covering all 10 canonical categories (`research`,
  `dev-tools`, `agency`, `marketing`, `content`, `automation`, `code-review`,
  `productivity`, `data`, `ops`).
* 50 paraphrased natural-language queries (committed in `tests/test_recall.py`).
* **Top-3 accuracy: 50/50 = 100.0%** (hard gate ≥ 70%).

## BM25 fallback

* **Wired:** YES. Ranking always blends BM25 (`score_bm25`) with vector
  similarity. If `sentence-transformers` weights fail to load, `embed_text`
  silently degrades to a deterministic hash-based 384-dim signature, and the
  per-row BM25 (title-weighted) carries the ranking. The route reports
  `backend: "bm25"` and `used_fallback: true` in that case.
* On Postgres, the production deployment can swap the per-row BM25 for
  `func.ts_rank(to_tsvector('english', title || description), websearch_to_tsquery(:q))`
  for index acceleration — the scorer interface is pluggable so a route-level
  optimisation requires no API change.

## Test results

```
tests/test_recall.py ............                                       [100%]
tests/test_recall_mcp.py ...                                            [100%]
12 + 3 = 15 new tests, all green
```

Full suite: 741 passing (was 725 at branch baseline; +16 new tests). No
regressions; the 10 failed / 15 errored items are the pre-existing
`tests/migrations/*`, `tests/test_auth.py::TestGitHub*`, `tests/test_sandbox.py::*`,
and `tests/test_skill_quality_gate.py::test_recipes_skill_repo_publish_mode_clean`
issues that were already failing on `main`.

## Notes

* The model column is `Text` rather than a SQLAlchemy `Vector` type so the same
  ORM works on Postgres + SQLite. The migration adds `vector(384)` natively on
  Postgres when `pgvector` is present; reads go through `_decode_embedding`
  which accepts both list and JSON-string shapes.
* Master keys (no `api_key_user_id`) see all tiers and never get
  `install_status: "tier_locked"`. Free callers passing a wider
  `tier_filter` get the higher-tier hits flagged as `tier_locked` so the
  agent can prompt an upsell.
