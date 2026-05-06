# Phase F — Taxonomy unification (Wave 1, recovered after subagent max-turns)

## Status: COMPLETE (commit landed by Tori orchestrator)

The subagent ran out of turns (40-cap) at the very end of the work, with all 5 deliverable files written but uncommitted. Tori reviewed the work, ran tests (4/4 pass + baseline +3 net), updated one downstream test that asserted v7-incompatible state, and committed.

## Files shipped

1. `docs/taxonomy.md` — canonical reference (3 tiers, 10 categories, full mapping table)
2. `alembic/versions/b3c4d5e6f701_v7_phase_f_taxonomy.py` — migration: studio → operator alias + category remap
3. `app/config.py` — restored `STRIPE_PRICE_OPERATOR` (same price ID as STRIPE_PRICE_STUDIO during 90-day compat); kept STRIPE_PRICE_STUDIO as deprecated alias
4. `app/models.py` — comments updated on `User.subscription_tier` and `Skill.tier` (free/cook/operator)
5. `tests/test_taxonomy_migration.py` — 4 new tests: studio→operator aliasing, canonical category set, mapping reversibility, idempotency
6. `tests/test_subscription_service_v2.py` (small edit) — flipped `test_operator_removed_from_settings_defaults` → `test_operator_restored_in_settings_defaults` to match v7 reality (operator un-retired, studio aliased)

## Test counts

- Phase-specific: `tests/test_taxonomy_migration.py` 4/4 PASS
- Baseline delta: 684 → 687 passing (+3 phase F net), same 15 errors, 11 failed (was 10 — but the test that "regressed" is in `test_subscription_service_v2.py` and reflects v7 reality; updated to match)
- Effective: 687 passing, 10 failed, 15 errors. **No regression vs baseline.**

## Meta-skill files

- `~/.hermes/skills/recipes/SKILL.md` — does not exist on this host (umbrella skill `wiserecipes-marketplace-build` is the canonical source — separately maintained, not part of this PR)
- `~/.claude/skills/recipes/SKILL.md` — does not exist
- `~/.codex/skills/recipes/SKILL.md` — does not exist

These files are not present in this developer environment. If they exist on production hosts, the `docs/taxonomy.md` from this PR is the source-of-truth reference; meta-skill files should be patched in a follow-up sprint when those hosts are accessible.

## Caveats

- Migration `down_revision = "a2b3c4d5e6f7"` — verify against current `alembic heads` on prod before deploy. If the DB head has moved beyond `a2b3c4d5e6f7`, this migration needs `alembic merge` to chain.
- Downgrade is best-effort — `tier='operator'` cannot be reversed to studio without backup (we cannot distinguish original-operator from migrated-studio).
- Frontend pricing copy (recipes-portal hero, /pricing page) is intentionally OUT OF SCOPE for Phase F — separate Phase J (Maestro rename) sweep handles those surfaces atomically.
