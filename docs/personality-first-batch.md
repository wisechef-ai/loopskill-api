# Personality first-batch spec (persona_factory_0821)

Grounded 30-candidate first batch for the LoopSkill personality catalog.
Adam floated a long-term target of 1,000 personalities; this batch does
**not** attempt that scale. It is quality rails + a defensible seed set —
scale-up is explicitly gated on external adoption signal, not on this PR.

## 1. Data model + authoring path (recon)

**Storage** — `app/models.py::Personality` (+ `PersonalityVersion` for
tarball/semver history, mirroring `Skill`/`Loop`). Fields: `slug` (unique,
indexed), `title`, `description`, `category`, `readme`, `license`, `tier`
(`free`/`pro`), `is_public`, `is_archived`, `creator_id`/`org_id`,
`system_prompt` (required, the actual SOUL/system-prompt body),
`config` (JSON — model/temperature/tool defaults), `install_count`,
`rating_avg`.

**API** (`app/personality_routes.py`, prefix `/api/personalities`):
- `GET /api/personalities` — browse public, non-archived personalities
  (`?q=`, `?category=`, `?limit=`).
- `GET /api/personalities/{slug}` — detail incl. `system_prompt` + `config`.
- `POST /api/personalities` — publish (auth required: `user` or `master`
  scope; validates `system_prompt` non-empty, slug uniqueness).
- `GET /api/personalities/external` + `/external/{slug}/install` — an
  off-by-default federated bridge to an "agency-agents" source, leak-scanned
  on install.

**MCP** (`app/mcp/tools/loopskill_catalog.py`): `loopskill_search_personalities`
and `loopskill_get_personality` — same authz path
(`authz.can_read_personality`), same public/non-archived filtering, no
existence oracle for private/nonexistent slugs (both return an identical
404-shaped error).

**Portal**: `app.loopskill.io/personalities/{slug}/` — Astro static page,
build-time `getStaticPaths`, live-verified 200 for both existing slugs.

**How the 2 existing personalities were authored**: hand-written directly as
Python dict literals in `scripts/seed_starter_catalog.py::STARTER_PERSONALITIES`
and inserted via `_seed_personalities()` (idempotent, slug-keyed skip). There
is **no UI publish flow that has ever produced a third personality** — the
`POST /api/personalities` route exists and is auth-gated, but the only two
live rows (`research-analyst`, `focused-dev-agent`) came from the seed
script, not a real publish. **No `member_skills` / `recommended_skills` field
exists in the schema today** — `config` is a free-form JSON blob (model,
temperature, default_tools observed in the 2 live rows). The validator
(§2) checks `config.recommended_skills` / `config.member_skills` on live
records as a forward-compatible convention it introduces, and validates
`member_skills` directly on not-yet-published candidate records — this batch
proposes it as the field a real publish flow should carry so a persona is
provably grounded in the skill catalog, not just a system-prompt string.

## 2. Validator: `scripts/personality_validate.py`

Mirrors `scripts/bundle_validate.py` (PR #267, merged) conventions exactly:
dataclass reports, `_get()` transport shim with `# noqa: BLE001` blanket
except + Rationale comment, 429-aware retry-once-then-WARN, JSON/text dual
output, `0/1/2` exit contract. Two input modes:

- `--all-public` / `--slug` — validates already-published rows over the live
  API (read-only, anonymous — same view a visitor gets).
- `--candidates <file.json>` — validates a **not-yet-published** batch (this
  spec's 30 candidates) so a bulk-seed script can be proven clean before a
  single row is written to prod.

**Gates**: G1 required-fields present+non-empty, G2 honest-description
(min length, placeholder-pattern reject, cross-batch duplicate-description
reject — the copy-pasted-persona signature), G3 every referenced skill slug
resolves live + is public + non-archived (candidates require >= 3 member
skills — a persona with fewer isn't a grounded role), G4 slug matches the
publish schema pattern and is unique within the batch.

Tests: `tests/test_personality_factory_rails.py`, 20 cases, RED+GREEN per
gate, same Router/monkeypatch strategy as `test_bundle_factory_rails.py`.

**Live-run proof** (recorded here per PR body convention):
```
$ python scripts/personality_validate.py --all-public
[PASS] research-analyst (0 referenced skill(s))
    ⚠ G3: no recommended_skills declared in config (not verified)
[PASS] focused-dev-agent (0 referenced skill(s))
    ⚠ G3: no recommended_skills declared in config (not verified)
2/2 personalities passed

$ python scripts/personality_validate.py --candidates docs/personality-first-batch-candidates.json
[PASS] git-workflow-copilot (4 referenced skill(s))
... (30 lines) ...
30/30 personalities passed
```

## 3. Demand signals mined

Three real sources, all live at authoring time (2026-08-21):

**(a) Live skill catalog categories** — `GET /api/skills/search?page_size=100`
returned 57 skills across 11 categories: automation (8), marketing (8),
ops (8), research (7), data (6), productivity (5), dev-tools (5),
code-review (3), content (3), agency (1), uncategorized (3). Every
category with >= 1 live skill and no existing persona wrapper is an
unwrapped-persona gap; candidates 11-30 are grounded here (e.g.
`system-architecture-planner` bundles 4 co-relevant `ops` skills that have
never been packaged together).

**(b) `~/.hermes/personalities/` on this machine** — checked, does not exist
(no local personality directory on this host). No signal available from
this source; noted honestly rather than fabricated.

**(c) `missing_skill_queries` in prod** (read-only `SELECT` via
`wisechef-hq`'s app venv, `WR_DATABASE_URL`) — 186 rows, unserved catalog
searches with zero result. Top real signal (excluding the synthetic
`zzzznotarealqueryzzzz` canary row, 16 hits, clearly a health-check probe):

| query | total hits | date range |
|---|---|---|
| voice | 11 | 2026-08-11 → 08-21 |
| git | 11 | 2026-08-11 → 08-21 |
| security | 11 | 2026-08-11 → 08-21 |
| image | 11 | 2026-08-11 → 08-21 |
| test | 11 | 2026-08-11 → 08-21 |
| email | 10 | 2026-08-12 → 08-21 |
| scrape | 10 | 2026-08-12 → 08-21 |
| calendar | 10 | 2026-08-12 → 08-21 |
| pdf | 10 | 2026-08-12 → 08-21 |
| docker | 10 | 2026-08-11 → 08-20 |
| api | 10 | 2026-08-11 → 08-20 |
| search | 8 | 2026-08-11 → 08-20 |
| copywriting | 7 | 2026-07-13 → 08-10 |
| database | 7 | 2026-08-12 → 08-21 |
| data | 7 | 2026-08-11 → 08-21 |
| humanizer | 4 | 2026-07-26 → 08-10 |

Candidates 1-10 map directly to these: `git-workflow-copilot` (git, 11),
`security-reviewer` (security, 11 — flagged as **partial-coverage**, no
dedicated security-scan skill exists in-catalog yet), `test-coverage-guardian`
(test, 11), `image-asset-producer` (image, 11), `voice-content-producer`
(voice, 11), `inbox-triage-assistant` (email, 10 — also partial-coverage),
`web-research-scraper` (scrape 10 + search 8), `document-digitizer` (pdf, 10),
`conversion-copywriter` + `ai-text-humanizer` (copywriting 7, humanizer 4 —
both directly name existing catalog skills that have zero persona wrapper).

**Honest gaps not covered**: `calendar`, `docker`, `database`, `api` have
sustained double-digit unserved demand but **no matching skill exists in the
live catalog at all** — wrapping a persona around them now would produce a
phantom (system prompt promising a capability with zero backing skill,
exactly the class G3 exists to catch). These are flagged as skill-catalog
gaps for a future skill-authoring pass, not persona candidates in this batch.

## 4. The 30 candidates

See `docs/personality-first-batch-candidates.json` for the full machine-
readable spec (slug, name, one-line role, target_user, member_skills,
why/grounding-evidence per candidate — the exact shape
`personality_validate.py --candidates` consumes). Summary:

| # | slug | category grounding | demand signal |
|---|---|---|---|
| 1 | git-workflow-copilot | dev-tools/code-review | missing_skill_queries: git (11) |
| 2 | security-reviewer | code-review | missing_skill_queries: security (11, partial) |
| 3 | test-coverage-guardian | dev-tools | missing_skill_queries: test (11) |
| 4 | image-asset-producer | content/dev-tools/marketing | missing_skill_queries: image (11) |
| 5 | voice-content-producer | productivity/marketing/content | missing_skill_queries: voice (11) |
| 6 | inbox-triage-assistant | content/marketing | missing_skill_queries: email (10, partial) |
| 7 | web-research-scraper | research/automation/data | missing_skill_queries: scrape (10) + search (8) |
| 8 | document-digitizer | marketing/productivity | missing_skill_queries: pdf (10) |
| 9 | conversion-copywriter | (uncategorized)/marketing | missing_skill_queries: copywriting (7) |
| 10 | ai-text-humanizer | (uncategorized)/marketing | missing_skill_queries: humanizer (4) |
| 11 | infra-bootstrap-engineer | automation | catalog: super-memory is #1-installed skill (361), unwrapped |
| 12 | diagram-and-spec-drafter | automation | catalog: 3 co-used automation skills, unwrapped |
| 13 | offer-and-launch-strategist | marketing | catalog: unpaired marketing trio |
| 14 | explainer-video-producer | marketing/dev-tools | catalog: unpaired rendering trio |
| 15 | system-architecture-planner | ops | catalog: 4 co-used ops skills, unwrapped |
| 16 | brand-rollout-coordinator | ops | catalog: unpaired ops trio |
| 17 | ruthless-plan-critic | ops | catalog: ruthless-mentor (11 installs), unwrapped |
| 18 | startup-diligence-analyst | research | catalog: unpaired research quartet |
| 19 | github-issue-triager | research/automation | catalog: github-issues unwrapped |
| 20 | academic-lit-reviewer | data | catalog: arxiv unwrapped |
| 21 | local-model-ops-advisor | data/marketing | catalog: unpaired local-inference trio |
| 22 | knowledge-wiki-curator | data | missing_skill_queries: data (7) + catalog: llm-wiki-hermes (11 installs) unwrapped |
| 23 | infographic-storyteller | productivity | catalog: baoyu-infographic unwrapped |
| 24 | codebase-onboarding-guide | productivity/ops | catalog: unpaired onboarding pair |
| 25 | frontend-prototyper | dev-tools | catalog: unpaired prototyping trio |
| 26 | social-post-formatter | dev-tools | catalog: xitter unwrapped |
| 27 | refactor-cleanup-specialist | code-review | missing_skill_queries: code/review (1 each) + catalog gap |
| 28 | meeting-notes-summarizer | content/productivity | catalog: unpaired summarize+pdf pair |
| 29 | executive-communicator | agency | catalog: minto is the ONLY agency-category skill, unwrapped |
| 30 | self-hosted-llm-infra-builder | (uncategorized)/data | catalog: buzz-mesh-linux-build unwrapped |

Every candidate carries 3-4 member skills, all resolved live against
`https://app.loopskill.io/api/skills/{slug}` at authoring time (proof:
§2's `--candidates` run, 30/30 PASS). No filler bios: each `role` +
`target_user` names a specific job, and each `why` cites a live catalog
category count, an unserved-query hit count, or both.

## 5. Explicit non-goals of this PR

- **No personalities created.** This PR ships the validator + this spec
  only. Publishing the 30 candidates (via `POST /api/personalities` or a
  future seed script) is a separate, later action gated on human review of
  this spec.
- **Not the 1,000 target.** ~100 is the near-term target Adam set; this is
  a 30-candidate first slice toward it, chosen for the strongest available
  grounding evidence, not padded to a round number.
- **No new `member_skills`/`recommended_skills` DB column.** The schema
  change to make skill-grounding a first-class, queryable field on
  `Personality.config` (or a new join table, mirroring `BundlePersonality`)
  is future work — flagged, not implemented, here.
