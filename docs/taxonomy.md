# LoopSkill taxonomy (v7 — locked 2026-05-06)

This document is the **single source of truth** for tier and category vocabularies
across the LoopSkill API, the WiseChef portal, the `loopskill` MCP/CLI (legacy
`recipes` tools still work via alias map), and meta-skill
SKILL.md files. Anything that disagrees with this file is a bug.

## Tiers

Currency is **USD**, never EUR (hub D-018 #3 — a prior version of this table
was wrong on both counts).

| DB slug    | Display label | Public (D-003)? | Stripe price env var           | Monthly price |
|------------|---------------|------------------|---------------------------------|---------------|
| `free`     | Free          | yes              | —                                | $0            |
| `pro`      | Pro           | yes              | `WR_STRIPE_PRICE_PRO`            | $9.95         |
| `pro_plus` | Pro+          | **no**           | `WR_STRIPE_PRICE_PRO_PLUS`       | $100          |

These three slugs (`free`, `pro`, `pro_plus`) are the **canonical tier vocabulary**
across the DB, API responses, SKILL.md frontmatter, and all tests.
The authoritative metadata (display names, badge colours, price IDs) lives in
`config/tiers.yaml` — edit only that file to change display labels or Stripe mappings.

### Public ladder = exactly 3 (autopilot_0308 M2, D-003)

`/pricing`, the tier picker, and all marketing copy show exactly **Free /
Pro / Enterprise**. `pro_plus` is **not** one of the three — it carries
`public: false` in `config/tiers.yaml` and is deliberately absent from
`config/recipes-marketing.yaml`'s `tiers:` block.

**`pro_plus` is NOT deleted. Do not delete it.** It stays a fully valid,
resolvable `db_slug` — `tier_labels.display_label()` and `bundle_limit()`
both keep working for it, and `subscription_tier='pro_plus'` remains a
perfectly normal value in the `users` table (it's a plain `String(32)`
column, not a DB enum, so there is no schema constraint to even relax).
Two reasons it has to stay live:

1. **The migration window (D-010).** 5 live accounts were on `pro_plus`
   when the ladder simplified. They migrate to `pro` via
   `scripts/migrate_pro_plus_to_pro.py` (dry-run by default; a human types
   a confirmation to `--execute`) — not instantly, and the row is `pro_plus`
   right up until that script actually runs each user.
2. **Enterprise contracts, indefinitely.** Enterprise (hub D-005: "anything
   above Pro is a sales conversation, not an automated meter") has no
   Stripe price object and no self-serve checkout — it is a contact form.
   There is no `enterprise` `db_slug` and none should ever be created for
   this purpose. An Enterprise-shaped customer is assigned the `pro_plus`
   `db_slug` directly by whoever closes that sales conversation.

**Do not "clean up" the `pro_plus` block in `config/tiers.yaml` or the
`pro_plus` value in the DB.** Dropping a `db_slug` with live rows on it is a
data-loss migration (autopilot_0308 premortem risk #2, L4×I10=40) — the fix
here was to hide it from *presentation*, which needed zero schema changes.

> **Legacy alias sunset — 2026-06-10:** The names `cook` (→ `pro`),
> `operator` (→ `pro_plus`), and `studio` (→ `pro_plus`) are accepted as
> backward-compat aliases in the `tier` query parameter of `/api/skills/search`
> and in `app/subscription_service.py` Stripe price-ID resolution.
> Both alias paths are removed on **2026-06-10**; callers must migrate to
> the canonical slugs before that date.  See `config/tiers.yaml` for the
> full alias mapping.

## Categories (10 canonical)

The catalog uses exactly these ten buckets. Every skill row's `category` column
must be one of these values; everything else is mapped during migration.

1. `research`     — discovery, knowledge harvesting, literature scans
2. `dev-tools`    — IDE helpers, code generators, CLI utilities for engineers
3. `agency`       — client deliverables, proposals, scoping, PM
4. `marketing`    — campaigns, SEO, ads, lead-gen
5. `content`      — copywriting, creative, image/video generation
6. `automation`   — workflow glue, schedulers, bots
7. `code-review`  — review, lint, audit, security scanning of code
8. `productivity` — personal workflow, email, calendar, notes, general utilities
9. `data`         — ETL, data extraction, analytics, ML pipelines
10. `ops`         — infra, devops, deployment, monitoring, platform

## Mapping (legacy → canonical)

Authored from the observed catalog values in `seed.py`, the dev-skills tarballs,
and the test fixtures across `tests/`.

| Legacy value         | Canonical bucket | Notes                                              |
|----------------------|------------------|----------------------------------------------------|
| `devops`             | `ops`            | infra/CI/CD                                        |
| `infrastructure`     | `ops`            |                                                    |
| `platform`           | `ops`            | seed.py "platform" rows                            |
| `monitoring`         | `ops`            |                                                    |
| `deploy`             | `ops`            |                                                    |
| `data-extraction`    | `data`           | seed.py scraper/ETL rows                           |
| `ml`                 | `data`           |                                                    |
| `analytics`          | `data`           |                                                    |
| `scraping`           | `data`           |                                                    |
| `etl`                | `data`           |                                                    |
| `creative`           | `content`        | seed.py image-gen rows                             |
| `copywriting`        | `content`        |                                                    |
| `video`              | `content`        |                                                    |
| `image`              | `content`        |                                                    |
| `seo`                | `marketing`      |                                                    |
| `ads`                | `marketing`      |                                                    |
| `growth`             | `marketing`      |                                                    |
| `email`              | `marketing`      | when used in marketing context                     |
| `reporting`          | `marketing`      | seed.py viral-skill rows; client reports           |
| `client-reporting`   | `agency`         | when explicitly an agency deliverable              |
| `consulting`         | `agency`         |                                                    |
| `proposals`          | `agency`         |                                                    |
| `development`        | `dev-tools`      | seed.py code-review-bot rows                       |
| `coding`             | `dev-tools`      |                                                    |
| `cli`                | `dev-tools`      |                                                    |
| `ide`                | `dev-tools`      |                                                    |
| `code-quality`       | `code-review`    |                                                    |
| `lint`               | `code-review`    |                                                    |
| `security`           | `code-review`    | static analysis / audit                            |
| `audit`              | `code-review`    |                                                    |
| `research-tools`     | `research`       |                                                    |
| `discovery`          | `research`       |                                                    |
| `knowledge`          | `research`       |                                                    |
| `automation-tools`   | `automation`     |                                                    |
| `workflow`           | `automation`     |                                                    |
| `bot`                | `automation`     |                                                    |
| `scheduler`          | `automation`     |                                                    |
| `communication`      | `productivity`   | seed.py email-composer rows                        |
| `tutorial`           | `productivity`   | seed.py tutorial rows                              |
| `general`            | `productivity`   | the CLI default fallback bucket                    |
| `utility`            | `productivity`   | dev-skills/file-transformer                        |
| `test`               | `productivity`   | dev-skills/hello-sandbox                           |
| `finance-ns`         | `productivity`   | tests fixture; non-canonical                       |

Anything not in this table that surfaces in a future migration audit defaults to
`productivity` (lowest-risk fallback bucket), and a follow-up PR adds the explicit
mapping. Never silently invent a new bucket.
