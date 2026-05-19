<!--
PR template for recipes-api. main is production — every merge auto-deploys.
Fill every section. Delete a section only if it genuinely does not apply,
and say why.
-->

## What & why

<!-- One paragraph: what does this change, and what problem does it solve? -->

## Type

- [ ] Bug fix
- [ ] New feature
- [ ] Refactor / cleanup
- [ ] CI / tooling
- [ ] Docs

## Testing

<!-- How did you verify this? Paste the relevant pytest output. -->
<!-- A bug fix MUST add a regression test. -->

- [ ] `pytest -q` passes locally (full suite green)
- [ ] `pre-commit run --all-files` passes
- [ ] New behaviour has a test; bug fixes have a regression test

## Production-impact checklist

<!-- main auto-deploys on merge. Confirm before merging. -->

- [ ] Touches a `CODEOWNERS` path (auth / authz / middleware / alembic /
      workflows / config)? If yes, a code owner has reviewed it.
- [ ] Adds an alembic migration? Confirmed single head (`alembic heads`).
- [ ] Changes pricing / tier labels? `config/tiers.yaml` is the only SSOT
      edited; DB slugs `cook` / `studio` unchanged.
- [ ] Edits a god node (`APIKeyMiddleware.dispatch`, `validate_key`,
      `recipes_install`, `SandboxRunner.run`, `scan_tarball`)? Blast radius
      understood and stated above.
- [ ] No secret, hostname, or real user name added to source or tests.

## Honesty check (marketing / claims surfaces only)

<!-- If this PR touches user-visible copy or capability claims, list: -->
<!-- Forbidden strings (claims removed) — must be ABSENT from prod after deploy -->
<!-- Required strings (claims added) — must be PRESENT in prod after deploy -->

## Linked issues

<!-- Closes #NNN -->
