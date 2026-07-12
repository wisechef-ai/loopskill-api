# P2 BRIEF — Follow-a-bundle (liked_0711)

IMPLEMENTER phase P2 on `wisechef-ai/loopskill-api`. Opus reviews before merge. Do not self-merge.
DEPENDS ON P0 (merged) + P1 (merged): Liked bundle exists; GET /api/library returns a
`followed_bundles` array (empty until this phase fills it).

## OQ-1 RESOLVED (default): follow = read-only saved reference, NOT content-merge.
Hearting/following a BUNDLE saves a reference to it; it does NOT copy its skills into your Liked.

## GROUND-TRUTH
- `Bundle` model models.py:827; `bundle_owner` col 846; `visibility` col 879 ('private'|'team'|'public').
- Only PUBLIC bundles (visibility='public') are followable. Reject follow of private/team → 403.
- Deploy/reconcile path already reads a bundle's joins onto an agent — reuse it for read-only deploy.

## SCOPE (one PR)
1. **`FollowedBundle` model + migration** — table `followed_bundles`, cols
   `(id uuid pk, user_id uuid not-null indexed, bundle_id uuid fk bundles.id not-null, followed_at ts)`,
   unique `(user_id, bundle_id)`. Chain migration off current head (`alembic heads`).
2. **POST /api/bundles/{id}/follow** — create the row. Only visibility='public' bundles;
   else 403. Idempotent (re-follow = 200 no-op). Cannot follow your own bundle → 400 (it's
   already yours). Returns `{ "following": true, "bundle_id": "..." }`.
3. **DELETE /api/bundles/{id}/follow** — remove row, idempotent 200. Returns `{ "following": false, ... }`.
4. **Fill GET /api/library `followed_bundles`** (the P1 contract left it empty): each entry
   `{ id, slug, name, owner_handle, followed_at }` per P4 §4. Resolve owner_handle from the
   bundle owner's user record (find how other routes surface a handle/display name).
5. **Read-only deploy of a followed bundle**: a follower can deploy a followed bundle onto an
   agent (reuse existing deploy path) but CANNOT edit it — a write/edit attempt on a
   not-owned, followed bundle returns 403. Free tier: unlimited follows; Liked stays the only
   editable bundle. Verify the existing edit-authz already denies non-owners (it should via
   `_resolve_owned_cookbook`); add a test proving a followed bundle is read-only.

## ACCEPTANCE
- Free user follows a public bundle, it appears in GET /api/library followed_bundles, deploys
  read-only to an agent, edit attempt = 403, Liked stays only editable bundle. Test the whole chain.
- Follow of a private/team bundle = 403; follow own bundle = 400; both idempotent.
- pytest green (no regression on the post-P1 baseline); coverage ≥85% changed; `ruff format app/`
  BEFORE commit (CI runs ruff format --check) + `ruff check app/` clean; pre-commit clean.

## DISCIPLINE
- ONE PR, branch `feat/liked-follow-bundle-p2`. Never touch `.coveragerc`. Don't touch god nodes.
- `docs/deviations/2026-07-12-liked_0711-p2.md`. Push, open PR vs main titled
  `feat(liked): P2 — follow-a-bundle (read-only saved reference)`. PR body: changed files +
  how you proved read-only + deviation link. Do NOT merge. Final line `P2_DONE <pr-url>`.
