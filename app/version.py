"""Single source of truth for the application version.

Phase 0 of ``loopskill_activate_0701``: the version was previously duplicated
as a string literal in four modules (``main.py`` twice, ``health_routes.py``,
``core_routes.py``), which let the deployed instance report a stale number and
made "is the live box running the code we shipped?" unverifiable from
``/api/healthz``. Every deploy that changes behaviour MUST bump this constant
so the healthz probe can prove the cutover landed.

Full bump-by-bump changelog lives in docs/ops/version-bump-history.md — moved
out of this docstring (issue #357) once it grew to 600 lines and started
tripping the pyfile-size-check god-object gate on a module whose only job is
tracking a version string. Append new entries there, not here; this
docstring keeps only the current bump's one-liner.

fix/issue-357 (0.9.51): tests/_app_factory.py drift from create_app() fixed
(14 missing routers + personalities double-prefix bug) + this changelog
extraction. See docs/ops/version-bump-history.md for the full entry.
Verified against prod /api/healthz 0.9.50 before bumping.
"""

__version__ = "0.9.51"
