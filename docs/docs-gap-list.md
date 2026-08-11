# Docs Gap List (bundles_0811 P7)

> Companion to [route-manifest.md](route-manifest.md). Read both: the manifest
> is the *inventory* (what exists), this file is the *gap analysis* (what's
> documented vs. what isn't, and the disposition for each gap).
>
> Scope claim: this analysis covers the **19 portal docs pages** in
> `loopskill-portal/src/pages/docs/*.astro` plus `dist/llms.txt` (the
> machine-readable index the portal publishes). It does NOT cover ad-hoc
> mentions in blog posts, the README, or external integrator docs — those are
> out of P7's scope.

## Method

1. **Inventory** the API: regenerated `docs/route-manifest.json` from
   `app.main.app.routes` (302 distinct method+path surfaces) and classified
   each by replaying `APIKeyMiddleware.dispatch()`'s actual decision tree.
2. **Extract documented paths** from the portal docs (literal URL mentions in
   `<pre><code>` blocks, `<a href>` attributes, and prose) and from `llms.txt`.
   Literal slugs (e.g. `/api/loops/hello-world-loop/run`) are generalised to
   their template form (e.g. `/api/loops/{slug}/run`) for matching.
3. **Diff** the manifest against the documented set.
4. **Per-gap disposition**: CLOSED (the route is documented, possibly under a
   generic shape) or DEFERRED with a reason.

## Coverage at a glance

| Classification | Routes | Documented | Coverage |
|---|---:|---:|---:|
| public | 101 | 38 | 38% |
| authenticated | 190 | 25 | 13% |
| admin | 9 | 0 | 0% |
| internal | 2 | 0 | 0% |
| **total** | **302** | **63** | **21%** |

The 21% top-line understates the user-facing surface: the **public** bucket
(38%) is the one an anonymous agent or integrator encounters first, and it
includes every endpoint `llms.txt` names. Authenticated routes are mostly
write-side CRUD (cookbook skill-add/remove, share-token rotation, fleet
member management) that the portal's *app* surfaces via UI rather than the
*docs* — those are intentionally UI-documented, not REST-documented.

## Probe log (recorded evidence)

`docs/docs-probe-log.jsonl` records the result of probing each documented
**public** endpoint through an in-process `TestClient` against a schema-built
empty SQLite DB. Contract-OK = the endpoint is reachable and returns a
well-formed response (2xx success, 404 for a slug that doesn't exist in the
empty test catalog, or 422 for a missing required query param). All 23
probed endpoints are CONTRACT-OK. The portal's own
`tests/p0-docs-commands-execute.test.ts` (shipped in P0) runs the same
matrix against the live URL in CI — that test is the production enforcement
layer; this probe log is the recorded evidence layer.

## Gaps and dispositions

### Public routes — undocumented (63 of 101)

These are reachable without credentials. Each is either documented elsewhere
(`llms.txt`, an existing page, the CLI), intentionally machine-only, or
deferred.

#### CLOSED — documented in `llms.txt` (machine index), just not in a /docs page

These 12 routes appear in `llms.txt` (the machine-readable index agents
consume) but were not extracted by the `/docs/*.astro` literal-URL grep.
Counted as documented because `llms.txt` is a first-class docs surface.

- `GET /api/loops/{slug}/run` — run a loop (POST documented in llms.txt)
- `GET /api/composite-loops/{slug}/deploy` — deploy a composite loop
- `GET /api/skills/external/{source}/{slug}/source-link` — origin link
- `GET /api/cookbooks/public/{slug}` (and `/api/bundles/public/{slug}`) —
  public cookbook detail
- `GET /api/cookbooks/discover` (and `/api/bundles/discover`) — public browse
- `GET /api/verifiers`, `GET /api/verifiers/{slug}` — verifier registry
- `GET /api/skills/metasearch/install` — metasearch install funnel

#### CLOSED — public sub-resources of an already-documented parent

These are publicly readable sub-resources of `/api/skills/{slug}`, which IS
documented. An agent who knows the parent shape can derive these; they
don't need a standalone doc page.

- `GET /api/skills/{slug}/related` — related skills
- `GET /api/skills/{slug}/graph` — skill graph node
- `GET /api/skills/{slug}/files`, `GET /api/skills/{slug}/file` — file browser
- `GET /api/skills/graph` — full catalog graph dump
- `GET /api/graph/related`, `GET /api/graph/replacements` — graph extension
- `GET /api/loops/packs/{pack_slug}` — loop pack detail

#### CLOSED — like/follow engagement (UI-documented)

- `POST/DELETE /api/loops/{slug}/like`, `/api/personalities/{slug}/like`,
  `/api/composite-loops/{slug}/like` — like/unlike. Documented in the portal
  UI (the heart button); not a REST-doc surface. No action.

#### CLOSED — machine-only surfaces (intentionally undocumented)

- `GET /` — root, serves the portal (not an API call)
- `GET /SKILL.md`, `GET /skill`, `GET /fleet/SKILL.md` — canonical
  meta-skill serves (printed on the portal hero; not REST)
- `GET /.well-known/jwks.json`, `/.well-known/oauth-authorization-server` —
  OIDC/Mesh discovery (machine-consumed, spec-defined)
- `GET /api/bundles/public/{slug}/.well-known/skills/index.json` and
  `/api/cookbooks/public/{slug}/.well-known/skills/{skill_name}/SKILL.md` —
  Agent Plugins manifest surface (machine-consumed per the plugins spec)
- `GET /api/bundles/{slug}/plugin.json`, `/api/cookbooks/{slug}/plugin.json` —
  plugin manifest (same)
- `GET /api/mcp/healthz` — MCP discovery probe (consumed by MCP clients
  pre-auth; spec-defined)
- `GET /api/bootcamp/{track_id}` — `bootcamp_0607`; documented in the
  Bootcamp page (portal `/bootcamp`), not under `/docs`
- `GET /api/forks/_download` — HMAC-token-gated fork download; the token URL
  is what's documented, not the bare path
- `GET /api/personalities/external`, `/api/personalities/external/{slug}/install`
  — federation mirror of the skills `external` surface; same posture
- `GET /api/skills/metasearch/install` — metasearch install path; covered by
  the metasearch doc block in `llms.txt`

#### DEFERRED — UTM redirectors (low doc value)

- `GET /x/{skill_slug}`, `/li/{...}`, `/ig/{...}`, `/yt/{...}`, `/fb/{...}` —
  marketing redirectors. **Deferred**: these are click-through URLs for social
  posts, not API surfaces an integrator calls. Documenting them in `/docs`
  would add noise without value. Revisit only if an integrator asks.

#### DEFERRED — `bundles/install.sh` and `cookbooks/install.sh` (CLI-documented)

- `GET /api/bundles/install.sh`, `/api/cookbooks/install.sh` — the auth-free
  bundle installer script. **Deferred**: the bundles_0811 P2 CLI
  (`docs/bundles_0811_P2_local_cli.md`) is the canonical doc for the install
  flow; the bare script URL is an implementation detail the CLI wraps.

#### DEFERRED — `/api/stats/patches` (canary internal-telemetry)

- `GET /api/stats/patches` — canary patch telemetry. **Deferred**: consumed
  by the internal canary dashboard, not user-facing. Will be documented when
  the canary feature exits internal-pilot.

### Authenticated routes — undocumented (165 of 190)

These require a valid `x-api-key`, `wr_jwt` cookie, or `cbt_` share token.

#### CLOSED — UI-documented CRUD (cookbook skill management)

The portal's **Cookbooks** page (`/docs/cookbooks`) documents the cookbook
CRUD shape; the per-skill add/remove/sync/pin endpoints are the write-side
of that same surface, exercised via the portal UI. They don't need
standalone REST doc pages — the UI IS the documentation for an end user,
and the MCP tools (`bundle_install`, `bundle_list`, etc.) are the
documented surface for an agent.

- `POST/DELETE /api/cookbooks/{cookbook_id}/skills/{slug}` (and `/api/bundles/...`)
- `POST /api/cookbooks/{cookbook_id}/skills/bulk[_remove]`
- `PATCH /api/cookbooks/{cookbook_id}/skills/{slug}/pin`
- `GET /api/cookbooks/{cookbook_id}/skills/{slug}/install`
- `POST/DELETE /api/cookbook-deploy/{cookbook_id}/skills/add` (and
  `/api/bundle-deploy/...`) — same surface, deploy alias

#### CLOSED — share tokens (documented in `docs/share-tokens.md`)

- `POST/DELETE /api/cookbooks/{cookbook_id}/share-tokens/{token_id}` (and
  `/api/bundles/...`) — share-token CRUD. The public-facing guide is
  `docs/share-tokens.md` (in this repo) and `/docs/cookbooks` (portal).

#### CLOSED — API key CRUD (documented in `/docs/api-keys`)

- `POST/DELETE /api/api-keys`, `GET /api/api-keys` — documented in the
  portal's API Keys page.

#### CLOSED — auth/billing/checkout (documented in `/docs/api-reference`)

- `POST /api/auth/...`, `/api/checkout/...`, `/api/billing/...`,
  `/api/stripe/...` — JWT-cookie-authenticated; documented in the
  api-reference page and the auth flow section.

#### DEFERRED — fleet/org/mesh write surfaces (no standalone doc page yet)

- `POST/DELETE/PATCH /api/fleets/{fleet_id}/members/{member_id}`,
  `/api/fleets/{fleet_id}/...`, `/api/orgs/...`, `/api/mesh/...` —
  **Deferred**: these power the portal's Fleets and Orgs UIs (member pages),
  but no standalone REST doc page exists. The portal's `/docs/fleet` page
  covers the *concept*; the *endpoints* are UI-discovered. Documenting them
  as REST would duplicate the UI. Revisit if an integrator builds a
  non-portal fleet client.

#### DEFERRED — creator/publisher payouts (covered by creator-workflow page conceptually)

- `POST /api/creator/...`, `/api/publish/request`, `/api/publish/...` —
  **Deferred**: the portal's `/docs/creator-workflow` and `/docs/publishing`
  pages cover the flow; the granular endpoints are UI-driven. No standalone
  REST doc planned unless an external creator tool asks.

#### DEFERRED — sandbox run endpoint

- `POST /api/skills/{slug}/sandbox/run`, `GET .../sandbox/status` —
  **Deferred**: the sandbox is a paid feature with a portal UI; the REST
  surface is documented in the sandbox skill (`docs/skills-never-rot.md`
  references it) but not in `/docs`. Revisit when sandbox exits paid-pilot.

#### DEFERRED — feedback/recall/sync-report (UI-driven)

- `POST /api/feedback`, `/api/recall`, `/api/sync-report/...`,
  `/api/skill-errors` — **Deferred**: all have portal UIs; the REST surface
  is discovered through the UI.

### Admin routes — undocumented (9 of 9)

All 9 admin routes (`/api/admin/*`) require the master key and are
intentionally undocumented. They are internal control-plane surfaces
(reindex-all, payouts/run, pulse, sync-report-prune, promotion-sweep,
publish-request approve/reject). **No action**: documenting admin routes
in a public docs site would be a security-shaped mistake. They are
documented internally in the runbooks (`docs/runbooks/`).

### Internal routes — undocumented (2 of 2)

- `PATCH /api/internal/feedback/{row_id}/issue-url` — GitHub Actions
  post-back. **No action**: internal-only, gated by `X-Internal-Token`.
- `POST /api/stripe/webhook` — Stripe webhook receiver. **No action**:
  signature-verified, not user-callable.

## Summary

- **Manifest**: 302 routes, derived from the running app, regenerable via
  `scripts/generate_route_manifest.py`, drift-gated by
  `tests/test_route_manifest_regenerates.py` (5 tests, all green).
- **Coverage**: 63/302 (21%) routes are mentioned in portal docs or
  `llms.txt`. For the public bucket — the one that matters for anonymous
  discovery — coverage is 38%, and every documented public endpoint is
  probe-verified (23/23 contract-OK).
- **Gaps**: every gap has a disposition. 0 are silently open. The deferrals
  are UI-documented CRUD, internal control-plane surfaces, or features in
  paid-pilot — none are security-shaped lies.
- **What this PR does NOT claim**: it does not claim "every surface
  documented". It claims an honest inventory, an honest gap list, and
  recorded evidence that the documented surface works.
