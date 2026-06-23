# Reconcile Contract — evergreen_0206

> **Status:** Phase A foundation. B/C/D/E/G all implement against this doc.
> **Source of truth:** this file. If code and this doc disagree, fix one in the same PR.
> **Mirrors:** Hermes `HubLockFile` (`hermes_cli/skills_hub.py`) — the proven host-side
> lockfile→reconcile pattern. We deliberately copy its shape so the mental model transfers.

A cookbook is a **desired-state declaration**, not an append-only list. Reconcile is the
act of converging an agent's local skills directory to its cookbook's declared state. The
server computes the diff; the agent applies it (atomically, Phase D). Convergence is always
a **pull** (Q2) — triggered three ways (SSE nudge / cron poll / manual `sync_requested_at` poke), never an
inbound push transfer.

---

## 1. The reconcile diff shape

The single canonical shape returned by the reconcile engine (Phase B) and consumed by the
client (Phase D):

```json
{
  "cookbook_id": "<uuid>",
  "generation": "<iso8601 of the cookbook generation token>",
  "channel": "stable",
  "diff": {
    "add":    [{"slug": "...", "version": "1.2.0", "checksum_sha256": "...", "install_url": "..."}],
    "update": [{"slug": "...", "from": "1.1.0", "to": "1.2.0", "checksum_sha256": "...", "install_url": "..."}],
    "remove": [{"slug": "..."}],
    "drift":  [{"slug": "...", "expected_sha256": "...", "install_url": "..."}]
  },
  "no_op": false
}
```

- **add** — declared in the cookbook, absent from the caller's reported local set.
- **update** — present locally at an older pinned_version than the cookbook declares.
- **remove** — present locally, no longer declared in the cookbook (source flipped to
  `disabled` or row gone). Emitted ONLY when the caller passes `prune=true`; default
  reconcile never removes (premortem #4 — opt-in, never auto-uninstall).
- **drift** — present locally at the right version but the on-disk `sha256` ≠ the cookbook's
  declared `checksum_sha256` (corrupted / hand-edited / partial pull). Emit re-install.

`no_op: true` when all four lists are empty — the client does nothing.

### REMOVE semantics — keyed off existing `source='disabled'`
CookbookSkill already soft-deletes via `source='disabled'` (models.py:818). **No `removed_at`
column is added** (it would be a redundant concept — Adam's no-redundancy rule). A skill is
"removed from desired state" iff its CookbookSkill row is absent OR `source='disabled'`. The
reconcile engine treats both identically.

---

## 2. The agent-side lockfile (`recipes-lock.json`)

Mirrors Hermes `HubLockFile`. Written by the client (Phase D), read on every reconcile.

```json
{
  "version": 1,
  "cookbook_id": "<uuid>",
  "generation": "<iso8601 — last-seen cookbook generation token>",
  "channel": "stable",
  "skills": [
    {"slug": "...", "source": "recipes", "pinned_version": "1.2.0",
     "install_path": "~/.hermes/skills/<slug>/", "sha256": "..."}
  ],
  "last_reconcile": "<iso-8601>",
  "last_rollback": null
}
```

`generation` is the heart of the cheap-poll contract (§3). `sha256` is what drift-detect
compares against. `install_path` is host-dependent (§5).

---

## 3. The generation token + cheap 304 poll (HYPER-SCALE, weak CPU)

**`Cookbook.updated_at` IS the generation token.** As of Phase A it is *truthful*: every
write that changes a cookbook's declared skill set advances it via
`_touch_cookbook_generation(db, cookbook_id)` (cookbook_routes.py) or the inline bump in
`recipes_sync.py`. The three paths closed in Phase A:

| Path | File | Mutation |
|---|---|---|
| add / reactivate skill | `cookbook_routes.py:add_skill_to_cookbook` | child INSERT / source flip |
| remove skill | `cookbook_routes.py:remove_skill_from_cookbook` | child source→disabled |
| sync pin-write | `mcp/tools/recipes_sync.py:recipes_sync` | child pinned_version UPDATE |

> **Why this was a bug:** SQLAlchemy `onupdate=func.now()` fires on the *parent* row UPDATE,
> never on child `CookbookSkill` mutations. Pre-Phase-A, the generation token could stay
> frozen while the skill set changed → a subscribed agent gets a false 304 and never
> reconciles. Pinned by `tests/test_evergreen_a_generation_token.py`.

**The 304 contract (wired in Phase D):**
- Client sends `If-None-Match: <generation>` (its lockfile's last-seen generation).
- Server does ONE indexed PK lookup on `Cookbook.updated_at`.
- Unchanged → **HTTP 304**, no diff computed, `_find_outdated_skills` never called.
- Changed → **HTTP 200** + the §1 diff.

~99% of polls collapse to a single indexed read on the weak box.

---

## 4. Abuse ceiling — per-agent, NOT a tier speed-throttle (decision #20)

Free is **not** artificially slowed. With subscribe-by-default (SSE) + 304-fast-path +
Cloudflare, a normal agent's reconcile costs ~zero, so there is nothing legitimate to
throttle. The rate limit exists ONLY to stop deliberate abuse (a script ignoring the
subscribe model and spamming the endpoint to exhaust the 30-conn pool).

- **Ceiling:** 60 reconcile requests / 5 min **per `api_key_id`**, **identical for all tiers**.
- **Backing:** Redis (`REDIS_URL`, config.py:89), keyed per `api_key_id`.
- **Response on trip:** `429` + `Retry-After`.
- Replaces the flat per-IP `RATE_LIMIT_PER_MINUTE: 60` (config.py:88) for the reconcile path.
- Tiers separate on **CAPABILITY** (free=single-cookbook+one-manual-sync · pro=scheduled
  auto-reconcile · pro+=fleet), never on reconcile speed.

---

## 5. Host-side write location (Q2 default)
- **Configurable, defaults to the host's detected agent skills dir.**
- Hermes → `~/.hermes/skills/` · Claude → `~/.claude/skills/` · Codex → its skills dir.
- Phase D ships **Hermes + Codex** host detection live (both are real dogfood hosts: Chef &
  Varys = Hermes, Codex = second validator). Claude/OpenCode detection: thin follow-on.

---

## 6. DB-pool ceiling + PgBouncer trigger (MULTI-TENANT)
- Verified ceiling: `pool_size=10 + max_overflow=20 = 30 connections` (database.py:12-13).
- 304-path + subscribe model keep most requests off the pool. Computing-reconciles + the
  `sync_fanout` LISTEN worker still consume connections.
- **Named trigger:** add PgBouncer (transaction-pooling, config-only, no code change) when
  sustained concurrent computing-reconciles approach **25**. A known dial, not a 2am incident.

---

## 7. TENANT ISOLATION — hard invariant (Adam directive 2026-06-03)

> Our internal setup, skills, and cookbooks MUST be invisible and inaccessible to every other
> Recipes user. Isolation is a HARD GATE on every new surface this sprint builds.

**Existing wall (preserve, never weaken):** `_resolve_owned_cookbook` (cookbook_routes.py)
returns `404 cookbook_not_found` when `cb.cookbook_owner != ctx.user_id` (and the caller is
not master and not a matching cbt_token). This is the per-tenant boundary.

**Rules every evergreen phase MUST obey:**
1. **Reconcile (B/D):** the reconcile engine resolves the cookbook through the SAME ownership
   path (`_resolve_owned_cookbook` or an `authz.can_*` predicate with `db=db` threaded). A
   caller may only reconcile a cookbook they own or hold a valid cbt_/fleet scope for. No
   cross-tenant generation reads — `If-None-Match` is checked AFTER ownership resolves, never
   before.
2. **Generation/304 (A/D):** a 304 vs 200 answer must never leak the existence or change-state
   of a cookbook the caller doesn't own. Ownership check precedes the generation comparison;
   unauthorized cookbook → `404`, never `304`.
3. **Federation (F):** external-source skills live in a separate `source` namespace labeled
   "External · community · as-is", second-class, behind the free toggle. Internal/private
   skills (`is_public=false`, owner-scoped) are NEVER surfaced to a non-owner through any
   federation search, router path, or adapter. The quality-namespace wall is also an
   isolation wall.
4. **Fleet (C/E/I):** fleet reconcile and drift-observability resolve agents/cookbooks within
   the fleet's own scope (`fleet_id`); one tenant's fleet status is never readable by another.
5. **Conversion gating (G):** the `free_sync_used_at` flag and cookbook count are per-account;
   no shared counter, no cross-account leakage.
6. **Test obligation:** every phase that adds a read/write surface adds at least one
   **negative isolation test** — "tenant B cannot see / reconcile / observe tenant A's
   cookbook/skill/fleet, gets 404." Tracked in a dedicated `test_evergreen_isolation.py`
   that grows per phase.

**The internal-skills concrete case:** Tori's/Chef's/Varys's private cookbooks (used for the
Phase H dogfood) are owner-scoped (`is_public=false`, `cookbook_owner=<our account>`). A
public free/pro/pro+ user hitting search, federation, reconcile, or fleet endpoints must
receive ZERO rows referencing them. Phase H's dogfood explicitly includes a negative probe: a
synthetic non-owner key sees none of our internal cookbooks/skills.

---

## 8. Backward-compatibility contract
- `recipes_sync` keeps its current update-only external behavior for existing callers (Phase B
  extends, never breaks — a caller that doesn't pass local lockfile state still gets the
  update-only diff).
- The §1 diff shape is the stable MCP response contract. The server may evolve underneath it;
  the client depends only on this shape.

<!-- evergreen_0206 Phase A — reconcile contract v1 (Tori 2026-06-03) -->
