# bundles_0811 P3.6 — personalities & MCP-servers (Connectors): scope decision

**Status: DECIDED.** Closes the gate left open by api#224 (bundles0811-P3.6).

The gate (plan §0 / Lock #9): *"Personalities and MCPs manageable through
the same bundle primitive, OR an explicit written decision that they are
out of scope for now."* This document is that decision. It also records the
inventory the previous worker started and did not finish.

## Inventory — what ships today

### Personalities
- **Model**: `Personality` (`app/models.py:2425`) — a deployable persona/SOUL
  (system prompt + config), its own table, own publish flow.
- **Read surfaces**:
  - HTTP: `GET /api/personalities`, `GET /api/personalities/{slug}`,
    `POST /api/personalities` (`app/personality_routes.py`).
  - MCP: `loopskill_search_personalities`, `loopskill_get_personality`
    (`app/mcp/tools/loopskill_catalog.py:90,124`) — both authz-gated via
    `authz.can_read_personality`, same not-found-on-forbidden shape as loops.
- **Bundle membership TODAY**: `BundlePersonality` (`app/models.py:2931`) —
  a dedicated join table (`bundle_personalities`): `bundle_id` +
  `personality_id` composite PK, `pinned_version`, `added_at`. **This
  already existed with production rows before this sprint** (Phase C /
  spotify_2607, per the comment at `bundle_routes.py:1741-1747`) — reconcile
  (`app/services/reconcile.py:554` `_declared_personalities`) and the
  liked-shelf read path (`app/library_service.py`) both already serve it.
  What Phase C added was the missing single-item HTTP write surface:
  `POST/DELETE /{cookbook_id}/personalities/{slug}` (`bundle_routes.py:1750-1813`).
- **What was still missing until this PR**: a BULK write surface (many
  personalities in one call) — every other "many members, one call" verb
  (`/skills/bulk`, `/skills/bulk_remove`) existed for skills only.

### Connectors (MCP-servers)
- **Model**: `Connector` — a named MCP-server config fragment (stdio/http/sse),
  versioned via `ConnectorVersion`. `ExternalConnector` is a **separate**,
  intentionally-empty-until-promoted staging table (`GET /api/connectors?include_external=true`)
  — confirmed this is by design (review gate), not a bug.
- **Read surfaces**: `GET /api/connectors`, `GET /api/connectors/{slug}`
  (`app/connector_routes.py`) — public, keyset-paginated.
- **Bundle membership TODAY**: `BundleConnector` (`app/models.py:2738`) —
  same shape as `BundlePersonality` (`bundle_id` + `connector_id` PK,
  `pinned_semver`, `added_at`). Single-item write surface already exists:
  `POST/DELETE /api/bundles/{id}/connectors[/{slug}]`
  (`app/connector_routes.py:254-328`). Reconcile already resolves declared
  connectors for deploy (`app/services/reconcile.py:247-357`,
  `bump_declaring_bundles_for_connector`).
- **What was still missing**: same gap as personalities — no bulk verb.

### `bundle_skills` (the skill primitive P3.6 built bulk ops on top of)
- Carries a two-way XOR identity: `skill_id` (local) **or**
  `federated_source` + `federated_slug` (federated), enforced by
  `ck_bundle_skills_local_xor_federated`. This XOR exists ONLY because a
  federated skill has no local row to point a foreign key at — it needed a
  second, string-pair identity scheme layered onto the same table.
  Personalities and Connectors have no federation concept today: every
  `Personality`/`Connector` a bundle can declare already has a real local row
  with a real `id`. There is nothing for a second identity branch to solve.

## The decision: (b) qualified — no schema unification, but the *verb*
## generalizes for free

**Personalities and MCP-servers (Connectors) are ALREADY manageable through
the bundle-membership primitive** — a Bundle has typed member tables
(`BundleSkill`, `BundlePersonality`, `BundleConnector`), each following the
identical shape (`bundle_id` FK + entity FK + optional pin + `added_at`),
each read by the same reconcile/liked-shelf code paths, each writable via a
dedicated single-item HTTP route. This is not a parallel, uncoordinated
second primitive invented late — Phase C explicitly chose "no new tables,
mirror `BundleSkill`'s shape" when it closed the write-surface gap, and this
PR does not revisit that call.

**What this PR explicitly declines to do, and why:** literally folding
`personality_id`/`connector_id` into `bundle_skills` itself (extending its
identity columns and `ck_bundle_skills_local_xor_federated` from a 2-way to
a 4-way XOR) is OUT OF SCOPE for this PR. That is a real migration on an
already-large, live-traffic table:
1. It requires new nullable FK columns on `bundle_skills` plus a rewritten,
   more complex N-way mutually-exclusive CHECK constraint.
2. Existing `BundlePersonality`/`BundleConnector` rows would need a data
   migration (or a long, error-prone dual-write period) to move into the
   unified table.
3. `bundle_skills` already carries skill-specific columns
   (`source` enum, `pin_mode`, `install_order`) that have no defined meaning
   for a personality or connector row — unifying tables would mean either
   those columns go NULL for 2/3 of the identity space, or personalities/
   connectors quietly grow skill-shaped columns they don't need.
4. None of this buys anything a caller cannot already do today via the
   sibling tables — it would be unification for its own sake, not a new
   capability.

Per the task's own instruction ("If (a) is a schema change of real size, (b)
with a crisp rationale is the honest answer") — this is that answer.

**What genuinely *is* cheap, and what this PR ships:** the bulk-operation
*verb* — many members declared/removed in ONE call, one commit, per-item
results — does not depend on `bundle_skills`' XOR machinery at all.
`BundlePersonality` and `BundleConnector` rows carry a single identity
column each (no XOR to satisfy — simpler than `BundleSkill`), so the same
bulk shape P3.6 built for skills (`add_skills_bulk`/`remove_skills_bulk`)
generalizes to them with **zero migration**: new endpoints, same pattern,
existing tables. This PR ships bulk add/remove for **personalities**
(`POST/POST /{cookbook_id}/personalities/bulk[_remove]`,
`app/bundle_routes.py`) as the concrete proof.

**Deferred, not silent:** bulk add/remove for **Connectors** is the
identical pattern (`BundleConnector` has the same single-column-identity
shape) and is left for a fast follow-up PR rather than expanded here, to
respect this sprint's time budget — not because it is harder or in doubt.
Anyone picking it up should mirror `add_skills_bulk`/`remove_skills_bulk`
onto `app/connector_routes.py`'s existing `declare_connector_in_bundle`/
`undeclare_connector_from_bundle` single-item logic; no schema work needed.

## What would have to be true to revisit true schema unification
Fold personalities/connectors into `bundle_skills` for real if, in a later
phase, `BundlePersonality`/`BundleConnector` start needing the SAME
per-row lifecycle features `BundleSkill` accrued over multiple phases
(a provenance/source enum, pin-vs-track semantics, install ordering,
federation) — at which point duplicating that logic identically across
three tables becomes the actual maintenance burden, not a hypothetical one.
Until that's observed, three small typed tables sharing one join-table
*shape* is simpler than one large table with a wide, mostly-NULL identity
fan-out.
