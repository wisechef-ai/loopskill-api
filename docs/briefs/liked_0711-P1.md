# P1 BRIEF — Typed heart API + recipes_like MCP verb + GET /api/library (liked_0711)

You are the IMPLEMENTER for Phase P1 on `wisechef-ai/loopskill-api`. A non-Codex reviewer
(Opus) reviews before merge — maker≠checker. Do not self-merge.

## DEPENDS ON P0 (already merged): `Bundle.is_liked` exists; every user has exactly one
undeletable Liked bundle; `app/liked_service.py:ensure_liked_bundle(db, owner_id)` returns it.
Use that helper to resolve the caller's Liked bundle — do NOT re-query by hand.

## FROZEN CONTRACT — build these EXACTLY (from P4 spec, ~/obsidian-vault/projects/recipes/plans/2026-07-11-liked-0711-P4-ui-spec.md §4)

### GET /api/library
Returns the caller's Liked bundle as three typed shelves + followed bundles.
Shape (NO count/total fields anywhere — hard rule):
```json
{ "liked_bundle_id":"uuid",
  "shelves": { "skills":[{"id","slug","title","liked_at"}],
               "personalities":[{"id","slug","title","liked_at"}],
               "loops":[{"id","slug","title","liked_at"}] },
  "followed_bundles": [] }
```
`followed_bundles` is an empty array in P1 (P2 fills it). Emit the key now so the contract is stable.

### POST /api/library/like   body `{ "type":"skill|personality|loop", "id":"uuid" }`
- Resolve caller's Liked bundle via `ensure_liked_bundle`. Add the artifact via the correct
  EXISTING join: skill→`BundleSkill` (models.py:902), personality→`BundlePersonality` (1868),
  loop→`BundleCompositeLoop` (1841). Reuse how bundle_routes.py already inserts these joins.
- Idempotent: re-like = 200 no-op (not 409). Returns `{ "liked":true, "type","id" }`.
- Free tier: the target is ALWAYS the caller's own Liked bundle (client sends no bundle_id).
- Validate `type` ∈ the three values → 422 otherwise. Validate the id exists → 404 otherwise.

### DELETE /api/library/like   body `{ "type","id" }` → unlike. Idempotent 200. Returns `{ "liked":false,... }`.

### recipes_like  MCP tool  (app/mcp/tools/, follow the existing recipes_* tool module pattern)
One tool, params `action: "like"|"unlike"`, `type`, `id`. Same semantics as the HTTP routes,
same AuthContext path as every other MCP tool (app/mcp/auth.py validate_key). Register it where
the other tools register. An agent must be able to like a skill, a personality, and a loop in
three separate calls and have all three appear in GET /api/library.

## ACCEPTANCE
- Agent hearts skill+personality+loop via MCP → all three appear in GET /api/library shelves →
  a reconcile/deploy pull of the Liked bundle carries all three onto the agent (verify the
  existing reconcile path already reads BundleSkill/BundlePersonality/BundleCompositeLoop — it does).
- Idempotency proven by test (double-like, double-unlike).
- Free-tier write-scope: a like never targets any bundle but the caller's Liked — test it.
- NO count/total field in any response body.
- pytest green (baseline 3381 + your new tests, no regression); coverage ≥85% changed lines;
  pre-commit clean.

## DISCIPLINE
- ONE PR, branch `feat/liked-heart-api-p1`. Never touch `.coveragerc`.
- Don't touch god nodes (validate_key etc.) beyond registering the MCP tool the standard way.
- `docs/deviations/2026-07-12-liked_0711-p1.md` — log every deviation.
- Push, open PR vs main titled `feat(liked): P1 — typed heart API + recipes_like MCP + GET /api/library`.
  PR body: changed files + how you proved the free-tier write-scope + deviation link. Do NOT merge.
  Final line: `P1_DONE <pr-url>`.
