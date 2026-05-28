# Recipes taxonomy (v7 — locked 2026-05-06)

This document is the **single source of truth** for tier and category vocabularies
across the Recipes API, the WiseChef portal, the `recipes` MCP/CLI, and meta-skill
SKILL.md files. Anything that disagrees with this file is a bug.

## Tiers

| DB slug    | Display label | Stripe price env var           | Monthly price |
|------------|---------------|--------------------------------|---------------|
| `free`     | Free          | —                              | €0            |
| `pro`      | Pro           | `WR_STRIPE_PRICE_PRO`          | €20           |
| `pro_plus` | Pro+          | `WR_STRIPE_PRICE_PRO_PLUS`     | €100          |

These three slugs (`free`, `pro`, `pro_plus`) are the **canonical tier vocabulary**
across the DB, API responses, SKILL.md frontmatter, and all tests.
The authoritative metadata (display names, badge colours, price IDs) lives in
`config/tiers.yaml` — edit only that file to change display labels or Stripe mappings.

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
| `seo`                | `marketing`      | tests/test_carousel_scoring.py uses this           |
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
