# P3 BRIEF — agency-agents as an external personality source (liked_0711)

IMPLEMENTER phase P3 on `wisechef-ai/loopskill-api`. Opus reviews before merge. Do not self-merge.

## OQ-3 RESOLVED (Adam's exact veto): DO NOT import/rehost. LINK to an externally-maintained
repo (`msitarzewski/agency-agents`, MIT, ~300 agents) so its personalities are DETECTED and
INSTALLABLE via LoopSkill, with ZERO content-maintenance burden on us. "Only use their fruits."
Someone else maintains the repo; we consume it.

## HONEST ARCHITECTURE NOTE (verified 2026-07-12 — read before you build)
The existing `app/services/federation*.py` layer is **skill-typed** (`ExternalSkill`) and
serves the metasearch/Browse skill surfaces. Personalities are served SEPARATELY and locally
from the DB via `app/mcp/tools/loopskill_catalog.py` (`loopskill_search_personalities`,
`loopskill_get_personality`). There is NO personality federation today. So P3 is NOT a drop-in
federation adapter — do NOT try to force personalities through `ExternalSkill`. Build a thin,
personality-native external source that mirrors the federation DISCIPLINE (injected fetch,
FETCH_ORIGIN-at-install, no rehosting, provenance) without touching the live skill-federation code.

## SCOPE (one PR) — the minimal correct build
1. **`app/services/agency_agents_source.py`** — a self-contained external personality source:
   - A pure parser `parse_agency_agents(raw_files: dict[str, str]) -> list[ExternalPersonality]`
     that maps `divisions.json` + each `<division>/*.md` (frontmatter + body) to a small
     `ExternalPersonality` dataclass `{slug, title, description, division, system_prompt,
     source_url, license}`. Pure function, unit-testable offline (NO network in the parser).
   - A fetch layer (network) INJECTED as a callable, same discipline as `GitHubTapAdapter`
     (fetch is a param so mapping is testable without hitting GitHub). Fetch reads the repo's
     files via the public raw GitHub host / trees API (TOKEN-FREE — these are public MIT files).
   - Every `ExternalPersonality` carries `license="MIT"` + `source_url` provenance to the exact
     repo file. Namespace label `source="agency-agents"`. These are ALWAYS second-class to
     internal personalities (mirror the ExternalSkill "external namespace, community · as-is"
     framing).
2. **Detection/browse**: surface these external personalities in the personality browse/search
   path (`loopskill_search_personalities`) behind the same external-source toggle skills use —
   OR, if that's too invasive, a dedicated `GET /api/personalities/external?q=` read endpoint
   that returns the parsed list. Pick the lower-blast-radius option and log the choice in the
   deviation log. Do NOT rewrite `loopskill_catalog.py`'s local path.
3. **Install = FETCH_ORIGIN at install time**: installing an external personality fetches its
   `system_prompt` body from the repo at install time (never rehosted, never pre-copied into
   our DB as a curated row). Leak-scan the body at the install boundary
   (skill-recipe-leak-audit discipline — reuse the existing scanner if one exists).
4. **Caching**: respect the existing metasearch/federation cache TTL pattern if you reuse it;
   otherwise a simple in-process TTL cache on the fetch is fine (the repo changes rarely). The
   point: upstream edits appear WITHOUT any action on our side, and we never run a sync job we
   have to maintain.
5. **Heartable**: an external personality must be likeable via P1's `recipes_like`
   (type=personality). Confirm the like path can reference an external personality id/slug —
   if the Liked join (`BundlePersonality`) requires a local `Personality` row, document the
   gap in the deviation log and propose the thinnest bridge (do NOT build a heavy
   materialization path without flagging it first).

## ACCEPTANCE
- ~300 agency-agents personalities are DETECTABLE + browsable via LoopSkill, each carrying MIT
  attribution + source URL, NONE rehosted as curated DB rows.
- An upstream change to the repo appears on our side with no code/sync action (cache TTL only).
- Parser is pure + unit-tested offline against a small fixture (2-3 fake division files).
- Install fetches from origin + leak-scans; no internal info leaks.
- pytest FULL suite green; `ruff format app/` before commit; `ruff check app/` clean; if you add
  an MCP tool it MUST carry an authz gate (secfix_1905-B) and keep server.py ≤600 (god-object cap).

## DISCIPLINE
- ONE PR, branch `feat/liked-personality-source-p3`. Never touch `.coveragerc` or the live
  `app/services/federation*.py` / `metasearch*.py` skill code (P3 is personality-native, separate).
- `docs/deviations/2026-07-12-liked_0711-p3.md` — log EVERY deviation, especially the browse-
  surface choice (#2) and the heartable-bridge decision (#5).
- Push, open PR vs main (mark ready, not draft), titled
  `feat(liked): P3 — agency-agents external personality source (link, not rehost)`.
  PR body: changed files + the two design choices you made + deviation link. Do NOT merge.
  Final line `P3_DONE <pr-url>`.
