# ADR: codebase size vs. adoption (answers #68)

**Date:** 2026-08-11
**Status:** Accepted — cut the number, don't defend it

## The honest numbers (re-measured, not the issue's stale count)

Issue #68 said "41,461 lines / 714 files / 59 tables / 79 migrations for 1
star." Those figures are stale. Measured on this branch, same day:

| Metric | Issue #68 said | Actual (2026-08-11) |
|---|---|---|
| `app/` Python files | 714 (repo-wide file count) | **289** |
| `app/` lines | 41,461 | **69,172** |
| Alembic migrations | 79 | **107** |
| Test files | (not stated) | **404** |
| GitHub stars | 1 | **2** |
| GitHub forks | 0 | **0** |

The trend since #68 was filed is **up**, not down: more lines, more
migrations, roughly flat adoption. That is the actual finding, and it is
worse than the issue as filed. Restating the old numbers here would have
been dishonest; the check in `cli/tests/test_loopskill_cli.py` and the
README claims check (`tests/test_readme_claims.py`) exist so numbers like
these can't silently drift out of sync with a written claim again.

## Why this happened

This repo grew out of `recipes-api` — a working recipe-search product with
Stripe billing, OAuth, a sandboxed loop runner, and federation already
built and tested (2,781-test green baseline at the time of the rename).
The registry was layered *onto* that, not built fresh. Keeping the
battle-tested subsystems (auth/authz, Stripe, sandbox, federated skill
search) was the right call for correctness — rewriting a working payment
and auth stack from scratch to save lines would have been a worse trade.
But the result is a codebase sized for a multi-tenant SaaS with billing,
not for the actual adoption signal, which is 2 stars and 0 forks.

## Decision: cut, don't justify

We are **not** going to write a paragraph arguing that 69K lines is fine
for what it does. It isn't fine for 2 stars. Justifying the size is the
defensive move; the honest move is to say what's getting cut and by when.

Committed cuts, tracked as separate issues (linked from #68):

1. **Extract the Stripe/billing/creator-payout subsystem** out of the
   always-loaded `app/` import path. Self-hosters who never take payments
   (the overwhelming majority of a self-host audience) should not have
   Stripe SDK, webhook routes, and payout models sitting on their
   attack surface and mental model on day one.
2. **Extract the sandbox runner's tier-specific integrations** at the
   provider boundary, not lump them into shared modules — sandbox stays,
   its client-specific glue doesn't need to.
3. **No new tables, no new migrations, until stars/forks show adoption**
   past today's 2/0. Feature requests that need new persistent state get
   scoped as "would we still add this at 0 users" before they're built.
4. **Module consolidation pass** on the god-object candidates already
   flagged by the repo's own 600-line cap — the cap exists, it should be
   used to shrink, not just to gate new code.

## What we are explicitly not doing

- Not rewriting the auth/authz/Stripe stack "for simplicity" — it's
  correct and tested; rewriting it would trade a known-good system for a
  new one with an unknown defect rate, for a codebase-size number that
  makes for a better README, not a better product.
- Not hiding the ratio. The README's [What's in the box](../../README.md)
  section states the file/line/migration counts inline with a test
  (`tests/test_readme_claims.py`) that fails the build if they drift from
  what's actually measured — so this table can't go stale the way #68's
  original numbers did.

## Follow-up

A comment linking this ADR was posted on #68. This document is the
answer; it does not close the issue — the cuts above are the actual work,
tracked separately.
