# Recipes API

**Recipes API** — skill marketplace backend for [recipes.wisechef.ai](https://recipes.wisechef.ai).

Mirrors LarryBrain's API surface (`x-api-key`, 60 req/min, `mode=files`) plus carousel + telemetry + WiseChef cross-sell.

Sister repo: [github.com/wisechef-ai/recipes-skill](https://github.com/wisechef-ai/recipes-skill) (public meta-skill).

---

## Overview

FastAPI-based backend powering the Recipes skill marketplace. It provides:

- **Skill search & discovery** — full-text + vector hybrid search across the skill catalog
- **Signed tarball downloads** — `mode=files` compatible with LarryBrain's Alexa API surface
- **Carousel** — featured/trending skill content per day
- **Telemetry** — anonymous install and usage event tracking
- **WiseChef cross-sell** — CTA + demo request funnel embedded in the marketplace
- **x-api-key auth** — `rec_` prefixed keys; centralized `auth_ctx` + `authz` layer (secfix_1905/A)
- **SQLAlchemy models** on a dedicated PostgreSQL database

## Module Layout (post-Phase-E)

```
app/
├── main.py                  FastAPI app factory + lifespan
├── config.py                Settings + boot-time secrets gate
├── auth_ctx.py              AuthContext frozen dataclass (scope, user_id, tier, …)
├── authz.py                 Authorization predicates (can_install, can_write_cookbook, …)
├── middleware.py            APIKeyMiddleware → populates request.state.auth_ctx
├── health_routes.py         GET /healthz + /api/healthz
├── skill_routes.py          GET /api/skills/* (search, trending, detail, related, external)
├── recipe_routes.py         GET /api/recipes/{slug} + /api/api-library/{slug}
├── install_routes.py        GET /api/skills/install + /api/skills/_download
├── access_routes.py         GET /api/skills/access + tier enforcement
├── utm_redirects.py         /x/, /li/, /ig/, /yt/, /fb/ short-link redirectors
├── _skill_helpers.py        _skill_to_out, _build_manifest, _count_today_installs, …
├── routes.py                Backward-compat re-exports (≤80 lines)
├── mcp/                     MCP server + tools (auth, install, recipify, sync, …)
├── sandbox/                 Skill execution sandbox (firejail / bwrap)
├── utils/client_ip.py       Trusted-proxy-aware real IP resolution
└── …                        (checkout, cookbook, creator, publisher, auth, etc.)
```

### auth_ctx flow

Every authenticated request flows through:

```
APIKeyMiddleware.dispatch()
  └─ validate_key(db, x-api-key)
       └─ request.state.auth_ctx = AuthContext(scope, user_id, tier, …)
            └─ REST routes / MCP tools / sandbox call authz.can_*() predicates
```

## Tech Stack

- **Python 3.11+** / FastAPI / Uvicorn
- **SQLAlchemy** ORM + PostgreSQL (`wiserecipes` DB)
- **Pydantic v2** schemas
- **itsdangerous** for signed download tokens
- Systemd user service on `wisechef-agents`

## API Endpoints (14 routes)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Root / health meta |
| GET | `/api/healthz` | DB health check |
| GET | `/api/skills/search` | Full-text skill search |
| GET | `/api/skills/trending` | Trending skills by install count |
| GET | `/api/skills/install` | Signed tarball download URL |
| GET | `/api/skills/_download` | Verify token + return tarball info |
| GET | `/api/skills/access` | Check skill access for caller |
| GET | `/api/skills/{slug}` | Full skill detail + versions |
| GET | `/api/recipes/{slug}` | Recipe detail |
| GET | `/api/api-library/{slug}` | API library entry |
| GET | `/api/carousel/today` | Today's carousel entries |
| GET | `/api/carousel/{date}` | Carousel by YYYY-MM-DD date |
| POST | `/api/telemetry` | Record telemetry event |
| GET | `/api/wisechef/demo-cta` | WiseChef cross-sell CTA |
| POST | `/api/wisechef/demo-request` | Submit demo request |

## Authentication

All endpoints (except `/`, `/docs`, `/redoc`, `/healthz`, `/api/healthz`) require:

```
x-api-key: rec_<32-hex-chars>
```

## Running Locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp wiserecipes-api.env.example .env  # fill in DB URL, secrets, etc.
uvicorn app.main:app --reload --port 8201
```

## Dev Toolchain (pre-commit + lint)

Install once:

```bash
pip install pre-commit
pre-commit install
```

Run locally (mirrors CI):

```bash
pre-commit run --all-files
```

Hooks: ruff (lint + format), bandit (security), mypy --strict (4 modules), actionlint, yamllint.

## Coverage gate

```bash
pytest --cov=app --cov-fail-under=85
```

Target: ≥ 85% line coverage on `app/`. Gate is enforced in CI (ci.yml).

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `WR_DATABASE_URL` | `postgresql://wisechef@localhost/wiserecipes` | PostgreSQL connection string |
| `WR_API_KEY` | `rec_dev_...` | Master API key (dev default) |
| `WR_SIGNING_SECRET` | `wr-tarball-...` | HMAC signing secret for download tokens |
| `WR_RATE_LIMIT_PER_MINUTE` | `60` | Requests per minute per IP |
| `WR_HOST` | `0.0.0.0` | Bind host |
| `WR_PORT` | `8201` | Bind port |

## License

Apache 2.0 — see [LICENSE](./LICENSE)

## Related

- **recipes-skill** — public Alexa/LarryBrain meta-skill: [github.com/wisechef-ai/recipes-skill](https://github.com/wisechef-ai/recipes-skill)
- **WiseChef** — AI workflow automation platform: [wisechef.ai](https://wisechef.ai)

## How to give feedback

The preferred way to submit feedback, recipe requests, or bug reports is via the
**MCP tools** bundled with the Recipes skill. They apply rate-limiting, dedup
detection, and automatically open a labelled GitHub issue.

### MCP tools

| Tool | When to use | Example |
|------|-------------|---------|
| `recipes_feedback` | UX issues, search problems, billing questions, docs gaps | See below |
| `recipes_request_recipe` | Ask for a new recipe / skill to be added | See below |
| `recipes_report_skill_error` | A recipe fails to install or run | See below |

**Send feedback:**

```
recipes_feedback(
    category="ux",
    message="The search results for whisper show the heavy model first — the light variant should rank higher.",
    context={"skill": "faster-whisper"},
)
```

**Request a new recipe:**

```
recipes_request_recipe(
    target_name="n8n self-hosted",
    why_useful="Enables no-code workflow automation on-prem without cloud lock-in.",
    suggested_sources=["https://github.com/n8n-io/n8n"],
)
```

**Report a broken skill:**

```
recipes_report_skill_error(
    slug="faster-whisper",
    signature="exit_code=1/missing-libcuda",
    summary="Setup fails on Ubuntu 22.04 without CUDA toolkit installed.",
)
```

### Rate limits

- Identical submissions (same signature) are deduped within 7 days.
- Per-tool limit: 10 distinct submissions / 24 h.
- Cross-tool ceiling: 30 total / 24 h across all three tools.
- Loop detector: >= 3 submissions in 5 min triggers a 15-min cooldown.

### GitHub UI fallback

If you cannot use the MCP tools, open an issue manually:

- [Recipe bug report](.github/ISSUE_TEMPLATE/recipe-bug.yml)
- [Recipe / skill request](.github/ISSUE_TEMPLATE/recipe-request.yml)
- [General feedback](.github/ISSUE_TEMPLATE/feedback.yml)

Issues opened via the MCP tools are automatically labelled and deduplicated.
GitHub UI issues require manual triage.
