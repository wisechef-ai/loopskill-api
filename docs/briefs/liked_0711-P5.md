# P5 BRIEF — Enforcement + funnel (liked_0711) — FINAL PHASE

IMPLEMENTER phase P5 on `wisechef-ai/loopskill-api`. Opus reviews before merge. Do not self-merge.
P0/P1/P2/P3 merged. This closes the Model-Y gate + upsell funnel.

## 🔴 #1 — CRITICAL BUG TO FIX FIRST (found in review 2026-07-12): Liked bundle breaks the free cap.
`app/bundle_routes.py:~688` counts the creatable-bundle quota as:
```python
existing = db.query(Bundle).filter(Bundle.bundle_owner == ctx.user_id).count()
if existing >= limit:  # free limit = 1
```
Since P0 auto-creates a Liked bundle for EVERY user, a brand-new free user already has
`existing == 1`, so `1 >= 1` → they can NEVER create their first real bundle. The Liked bundle
is a SYSTEM primitive (like `is_base`) and must NOT count against the user's creatable quota.
FIX: exclude system bundles from the count:
```python
existing = (
    db.query(Bundle)
    .filter(
        Bundle.bundle_owner == ctx.user_id,
        Bundle.is_liked.is_(False),   # Liked is a system primitive, never counts
        Bundle.is_base.is_(False),    # defensive: base is owner-less anyway
    )
    .count()
)
```
Then RED-PROOF: write a test where a free user (with an auto Liked bundle) creates their FIRST
real bundle successfully (200), and their SECOND is blocked (403 pro_tier_limit). Neutralize the
`is_liked.is_(False)` filter → the first-create test must flip to 403 → confirms the filter is
load-bearing → restore. This is the whole Model-Y economics; get it exactly right.
Audit EVERY other place that counts `bundle_owner ==` for quota/limit purposes and apply the same
system-bundle exclusion (grep `bundle_owner` across app/ — check checkout_routes.py, auth_routes.py
which surface cookbook_limit too). Log each site touched in the deviation log.

## #2 — Free = 1 API key, no parallel keys.
The billable primitive is the API key (free=1 → 1 bundle). Enforce: a free user cannot mint a 2nd
API key. Find the key-creation route (app/api_key_routes.py). If a free user already has 1 active
key and requests another → 403 with the upsell payload shape below. (If this gate already exists,
verify it and add a test; if not, add it.) Log what you found.

## #3 — 403 upsell payload consistency (P4 §5).
Every Model-Y gate 403 (2nd bundle, 2nd key, per-agent divergence) returns a CONSISTENT structured
detail the UI renders as a ≤2-click inline upsell (NOT a redirect, NOT a toast — mirrors the
existing `composer.astro showUpgradeWall()`). Standardize on:
```json
{"reason": "<pro_tier_limit|free_key_limit|per_agent_divergence>",
 "upgrade": {"tier": "pro", "cta": "/api/checkout/pro"}, "limit": <n>}
```
The existing `{"reason":"pro_tier_limit","max_cookbooks":limit}` at bundle_routes.py:688 should be
brought into this shape (keep `max_cookbooks` too for back-compat if any test asserts it — grep
first; do NOT break an existing contract, extend it).

## #4 — Free deploys the SAME Liked to all agents; per-agent divergence is a Pro gate.
Free users reconcile their ONE Liked bundle onto ALL their agents (same identity everywhere, L1).
An attempt to assign a DIFFERENT bundle to a specific agent (per-agent divergence) on free → 403
upsell + measured. Find the per-agent assignment path (reconcile / fleet). If per-agent assignment
doesn't exist yet as a distinct capability, the gate is: free may only deploy is_liked bundle to
agents; any deploy of a non-Liked owned bundle to a specific agent on free → 403. Log the exact
mechanism you found and gated.

## ACCEPTANCE
- Free user WITH auto Liked bundle can create exactly 1 real bundle (200), 2nd blocked (403). RED-proofed.
- Free user cannot mint a 2nd API key (403 upsell).
- Every Model-Y 403 carries the consistent upsell payload; no existing contract broken (grep tests first).
- Per-agent divergence on free → 403 upsell.
- pytest FULL suite green; ruff format before commit; ruff check clean; any new MCP tool authz-gated;
  server.py ≤600.

## DISCIPLINE
- ONE PR, branch `feat/liked-enforcement-funnel-p5`. Never touch `.coveragerc`. Before editing the
  bundle-create god node run a mental gitnexus_impact (it's a load-bearing path). Don't break the
  existing cookbook-cap tests — extend them.
- `docs/deviations/2026-07-12-liked_0711-p5.md` — log every quota-count site touched + every gate found/added.
- Push, open PR vs main (ready, not draft), titled
  `feat(liked): P5 — Model-Y enforcement + upsell funnel (+ fix Liked-counts-against-cap bug)`.
  PR body: the critical bug fix + RED-proof + every count site touched + deviation link. Do NOT merge.
  Final line `P5_DONE <pr-url>`.
