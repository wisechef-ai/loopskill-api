# Deviation — bundles0811-P1 follow-up: F4 gate (wire-or-delete follow)

Branch `agent/tori/bundles0811-p1-seed-follow`.

## The gate

Plan step 3 (verbatim): "Follow must do something visible. `followed_bundles = 0`
with a live table: either wire follow to a real update signal or DELETE the
surface (musk step 2 — a button that does nothing is worse than no button)."

## Starting recommendation (from the task brief)

DELETE. `follower_count` is 0 on all 5 public bundles, `followed_bundles` has
never had a single row in prod, and the brief's author found no consumer.

## What recon found before acting

`app/authz.py:can_reconcile_cookbook` (used by both the HTTP reconcile route,
`app/reconcile_routes.py:97`, and the MCP reconcile engine,
`app/services/reconcile.py:449`) grants a **follower** of a public bundle
read-only reconcile/deploy rights to that bundle: a follower's agent can pull
the bundle's desired state (`GET .../reconcile`, dry-run) and deploy it onto
their own fleet, but any *write* attempt (`dry_run=False`) 403s with
`read_only_follow`. This is not aspirational — it is exercised end-to-end by
a passing test on `main`:
`tests/test_liked_0711_p2.py::test_followed_public_bundle_is_listed_deployable_and_read_only`.

So `follow` already does something visible and real: it is the authorization
primitive behind "install/deploy a bundle you don't own, read-only." The
`followed_bundles=0` count in prod reflects zero *discoverability* (no portal
UI prominently offers a Follow CTA), not a dead / no-op surface.

## Decision: RETAIN, do not delete

Deleting `follow_routes.py` / `FollowedBundle` would:
- delete live, tested authz logic (`can_reconcile_cookbook`'s follower branch)
- regress a passing test (`test_liked_0711_p2.py`)
- remove the ONLY current path by which a non-owner can legitimately deploy
  a public bundle read-only

None of that squares with "reversible, low-risk deletion of dead code" — this
is deletion of *live* code with a real (if under-adopted) consumer. The
`followed_bundles=0` signal is a growth/marketing gap (nobody has been asked
to follow anything yet — no seeded bundles existed until this same PR), not
an architecture defect. Gate 2 of this same PR (seeding 5 public bundles) is
itself a direct first step toward giving `follow` something worth following.

## What changed as a result of this decision

Nothing in `app/` — this is a recon-only, no-code-change decision. A row was
added to `~/.hermes/state/deletion-ledger.tsv` (2026-08-11,
`loopskill-api:app/follow_routes.py + FollowedBundle surface`) documenting
the finding and the RETAIN verdict per the reversibility-ledger requirement,
even though no deletion occurred — so this gate has a paper trail like every
other musk-step-2 evaluation in the ledger, not just the ones that deleted
something.

## If this is wrong

If a future audit finds `can_reconcile_cookbook`'s follower branch is ALSO
dead (e.g. no MCP/reconcile client ever actually calls it either), re-open
this gate — the deletion candidate would then be the follower branch of
`can_reconcile_cookbook` + `follow_routes.py` + `FollowedBundle` together,
with `test_liked_0711_p2.py`'s follower-deploy assertions removed in the same
PR (never leave a green test asserting behavior that no longer exists).
