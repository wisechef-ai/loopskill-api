# Phase D — LOGIN FRONT-DOOR + kill WiseRecipes self-ID — design contract (loopskill_activate_0701)

Author: Tori. Implementer: subagent (independent of phases 1/A1/B — can run in parallel). Reviewer: codex.
Branch: loopskill_activate_0701/phaseD.

## Scope (small, surgical)

### D1 — Kill the WiseRecipes self-ID (3 locations)
- app/main.py:1 — module docstring "WiseRecipes API" -> "LoopSkill API"
- app/main.py:108 — FastAPI(title="WiseRecipes API") -> "LoopSkill API"
- app/main.py:213 — root() {"name": "WiseRecipes API"} -> "LoopSkill API"
Add a contract test (tests/test_activate0701_d_no_wiserecipes_self_id.py): assert
the root JSON + OpenAPI title contain "LoopSkill", NOT "WiseRecipes".

### D2 — Repoint default feedback dispatch off recipes-api
- app/github_dispatch.py:24 — `_REPO = "wisechef-ai/recipes-api"` is the DEFAULT repository_dispatch
  target. The user-routable PAT path (loopclose J) already works and overrides this.
  Change the default to `wisechef-ai/loopskill-api` (the renamed repo — same codebase,
  recipes-api is the legacy GitHub name still pointing at the same repo).
  Actually — verify first: does the GitHub repo `wisechef-ai/loopskill-api` exist and
  have the dispatch workflow? If not, keep `_REPO` pointing at the real repo and add a
  comment explaining the repo was renamed on GitHub (the `recipes-api` name may be the
  actual current name — grep `git remote -v` to confirm). DO NOT break dispatch by
  pointing at a non-existent repo. If the remote is still wisechef-ai/recipes-api,
  leave _REPO as-is + add a `# activate_0701/D` comment that the repo-name vs
  product-name divergence is known; the self-ID sweep (D1) is about the PRODUCT name
  surfaced to callers, not the dispatch target.
  Test: assert _REPO matches git remote origin (programmatic check the dispatch target
  is never stale).

### D3 — Hide dead Google OAuth (login front-door cleanup)
- The portal login page offers Google login. GOOGLE_CLIENT_ID is unset in prod (verify
  via ssh `grep GOOGLE_CLIENT_ID /home/wisechef/loopskill-api/.env`). Routes
  app/auth_routes.py:207+ (/google/login) return 503 when unset (read the handler).
  Two safe options: (A) Portal-side: hide the Google button when no client_id — but
  the portal is a separate repo and this phase is API-only; (B) API-side: gate the
  routes behind a 404 when GOOGLE_CLIENT_ID is unset (so the dead button gets a clean
  404 instead of a confusing 503). Go with (B) but ONLY if the portal login flow
  already checks for the button presence client-side — otherwise hiding the route
  breaks the button's error handler. SAFEST: read the portal login.astro; if it renders
  the Google button unconditionally, leave the routes (503 is already correct behavior
  for "provider not configured") and just add a clear error message. Document the
  decision in the test rationale.
  DEFAULT (if uncertain): leave Google routes as-is (503 path is already correct);
  focus this phase on D1 (self-ID) + D2 (dispatch target liveness contract). Note
  the deferral in the summary.

### D4 — UI key-mint (already exists — verify + surface)
- api_key_routes.py already has POST /api/api-keys + GET /api/api-keys + DELETE. The
  portal needs a page that calls these (the "UI key-mint" gate). This is PORTAL work
  (separate repo) — for the API phase, just verify the routes are wired correctly and
  add a smoke test that the key-mint round-trip works via the standard auth header
  pattern. If the portal already has the page, no action. NOTE in summary.

## Tests
tests/test_activate0701_d_self_id_and_dispatch.py:
1. GET / -> 200, JSON name contains "LoopSkill", NOT "WiseRecipes".
2. OpenAPI title contains "LoopSkill".
3. _REPO constant matches `git remote get-url origin` (parsed) — dispatch target never stale.
4. (Google route behavior preserved — whatever the decision, pin it in a test).

## Version
Bump app/version.py minor (check current value first; coordinate with parent at merge).
