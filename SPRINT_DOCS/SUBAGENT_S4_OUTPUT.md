# Stream 4 — Graphify integration & version pinning

## Summary

- Added a fourth signal to `app.edge_builder.build_edges`: `declared_relation`,
  driven by `[skill].related_skills` from skill_toml. Weight: 0.4 declared +
  0.4 jaccard + 0.1 category + 0.1 coinstall (sum 1.0).
- New helper `extract_related_skills(skill)` mirrors `extract_tags`.
- New `dry_run_compare(db)` returns old-vs-new edge sets, delta_pct, and a
  `breaking` bool (>20% delta is the controller's safety bar).
- Backwards compatibility: `build_edges(use_declared=False)` keeps the legacy
  weights so the dry-run gate can compare. The old call site (no kwarg) still
  works because `use_declared` defaults to True; existing 47 tests in
  `test_graph_extension.py`, `test_graph_dump_endpoint.py`,
  `test_install_count_sync.py` are unchanged.
- Version pinning: `recipes_install` MCP tool now accepts `slug@semver` or
  `version=` kwarg; `available_versions` returned on miss. Same support added
  on the REST `GET /api/skills/install?version=...` (and `?slug=foo@1.2.3`).
- `recipes_install` response includes `related_skills` (<=10) read directly
  from `SkillDerivedEdge` (no HTTP shell-out).

## Files changed

- `app/edge_builder.py` — extract_related_skills, build_edges(use_declared=),
  dry_run_compare
- `app/mcp/tools/install.py` — _split_slug_version, _related_slugs, recipes_install
- `app/routes.py` — install_skill (REST) accepts `version` query param + slug@semver
- `tests/test_graph_integration.py` — 8 new tests (related_skills extraction,
  declared_relation promotion, dry_run shape, version pinning happy path,
  unknown version, related surfacing)

## Tests

8/8 new tests pass; 47/47 existing graph + install tests still pass.

## Stream 4.0 verdict

Version pinning was NOT supported in the live API at sprint start. Stream 4.5
was folded INTO Stream 4 (no separate worktree) because the change is small
and self-contained. Adversarial test in Phase B can now use `slug@1.0.2`.
