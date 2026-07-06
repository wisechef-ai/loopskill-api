"""Single source of truth for the application version.

Phase 0 of ``loopskill_activate_0701``: the version was previously duplicated
as a string literal in four modules (``main.py`` twice, ``health_routes.py``,
``core_routes.py``), which let the deployed instance report a stale number and
made "is the live box running the code we shipped?" unverifiable from
``/api/healthz``. Every deploy that changes behaviour MUST bump this constant
so the healthz probe can prove the cutover landed.

fix/skill-artifact-identity: bumped past live prod (0.9.4, verified via
GET /api/healthz) and current main (0.9.4) — this PR rebrands the /skill
install artifact, no schema change.

feat/unified-search: bumped past live prod (0.9.5, verified via GET
/api/healthz) and current main (0.9.5) — this PR adds the new anonymous
GET /api/search endpoint, no schema change.

feat/bundle-detail-artifact-parity: bumped past live prod (0.9.6, verified via
GET /api/healthz) and current main (0.9.6) — bundle detail now returns declared
personalities + composite_loops sections, no schema change.
"""

__version__ = "0.9.7"
