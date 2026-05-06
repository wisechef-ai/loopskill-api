# Phase J — chef→maestro rename + skill_aliases (Wave 2, recovered after subagent max-turns)

## Status: COMPLETE (PR opened by Tori orchestrator)

The subagent ran 5 commits' worth of real deliverables (the migration, model, rename script,
SKILL.md content, and 301-redirect routes patch) before hitting the 40-turn cap. Tori
orchestrator added the test file (subagent skipped it), wrote this output doc, ran the
suite (6/6 phase tests pass + baseline preserved 725→731 passing), pushed, and opened the PR.

## Files shipped

| Commit | Files |
|--------|-------|
| `feat(v7/phase-J): add SkillAlias model for chef→maestro redirect` | `app/models.py` |
| `feat(v7/phase-J): migration for skill_aliases table + seed chef→maestro` | `alembic/versions/c7a8b9d0e1f2_v7_phase_j_skill_aliases.py` |
| `feat(v7/phase-J): idempotent chef→maestro rename script with audit log` | `scripts/maestro_rename_migration.py` |
| `feat(v7/phase-J): 301 redirect from old skill slugs via skill_aliases` | `app/routes.py` |
| `feat(v7/phase-J): author maestro + framework-v0 SKILL.md content` | `recipes/maestro/SKILL.md`, `recipes/framework-v0/SKILL.md` |
| `feat(v7/phase-J): tests for skill_aliases redirect (Tori recovery)` | `tests/test_maestro_rename.py` |

## Test counts

- `tests/test_maestro_rename.py`: **6 / 6 PASS**
  - Migration creates skill_aliases table
  - Insert/fetch round-trip works
  - GET /api/skills/chef returns 301 with Location header when alias active
  - GET /api/skills/chef returns 404 when alias expired
  - GET /api/skills/chef returns 404 when no alias (no regression)
  - old_slug uniqueness enforced (the property the idempotent rename relies on)
- Full suite: **731 passing**, 10 failed, 15 errors (same baseline as Phase F head — 725 + 6 net new = 731). No regressions.

## Out-of-scope deliverables NOT shipped (Phase J spec mentioned, deferred)

The 40-turn budget didn't reach these. They're tracked here for the next session or a follow-up:

- `app/mcp/tools/doctor.py` PATCH for Free-tier `stale_check` re-engagement loop (R9). Doctor tool exists from Phase A; the staleness CTA is purely additive and can be a small follow-up PR.
- The chef SKILL.md was not authored as a separate file (only maestro + framework-v0 were). If a `chef` skill row exists in production catalog, the `scripts/maestro_rename_migration.py` script handles the row rename + alias seed — but if Adam wants the new `chef` SKILL.md as a separate aliased reference, that's a follow-up.

## Adam manual handoffs (out of scope for this PR — track in #tori)

1. **Discord pinned message** in #recipes — manually update any `chef` references to `maestro`.
2. **recipes-portal hero copy** — separate repo (recipes-portal). Pricing page still references `Studio` label; v7/phase-F docs already noted that handoff. Same fix applies for any `chef → maestro` user-facing copy.
3. **Cognee skill-graph nodes** — if the cognee instance has indexed `chef` as a node label, run `cognee.delete_nodes(label='chef')` then `cognee.upsert(maestro_skill)` after this PR ships and the catalog row is renamed.
4. **Customer-side `~/.hermes/skills/chef/` shim** — for customers who already installed the `chef` meta-skill via `recipes_install`, add a one-line shim (or document the manual path in the maestro SKILL.md release notes).

## Caveats

- Migration `down_revision = "b3c4d5e6f701"` (the Phase F head). Verify against production `alembic heads` before deploying — if heads have moved, run `alembic merge`.
- The `chef → maestro` alias has a 90-day TTL (`expires_at` set in the migration's seed step). Past that window, `/api/skills/chef` will 404 honestly. Plenty of runway for v7.1 to roll out additional comms.
- The 301 redirect also fires for any future renames — the SkillAlias table is general-purpose, not chef-specific.
