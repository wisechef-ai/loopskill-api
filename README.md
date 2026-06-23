# LoopSkill API

**LoopSkill** — open-core, self-hostable skill registry for AI agents.
Browse, install, and deploy skills · bundles · loops · personalities. Zero signup to self-host.

---

## Run it locally in 60 seconds

**One-liner (Docker):**
```sh
curl -fsSL loopskill.io/install | sh
```

**Or clone + make:**
```sh
git clone https://github.com/wisechef-ai/loopskill-api
cd loopskill-api
make dev
```

No `.env`, no secrets, no Postgres required. The default stack uses SQLite and seeds a
starter catalog automatically. Your dev API key is printed on first boot.

See [docs/SELF_HOST.md](docs/SELF_HOST.md) for the full quickstart.

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
├── sandbox/                 Skill execution sandbox (firejail / bwrap, **Linux-only**)
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

Many endpoints are unauthenticated (carousel, skill search, skill detail, `_download`,
stats, marketing, MCP healthz, UTM redirectors, and JWT-based auth/billing/creator
routes). The full exemption list lives in `APIKeyMiddleware.EXEMPT_PATHS`,
`PUBLIC_PREFIXES`, and `JWT_AUTH_PREFIXES` in `app/middleware.py`.

Endpoints that **do** require an API key (install, publish, cookbook operations,
telemetry writes, admin tools) expect:

```
x-api-key: rec_<32-hex-chars>
```

## Running Locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp wiserecipes-api.env.example .env  # fill in DB URL, secrets, etc.
alembic upgrade head                  # apply all migrations before first start
uvicorn app.main:app --reload --port 8201
```

> **Alembic:** The app checks `alembic heads` alignment at startup via
> `app/startup_checks.check_alembic_heads()` and refuses to start in non-SQLite
> environments when migrations are behind.  Always run `alembic upgrade head`
> after pulling new commits.

> **pgvector (optional):** The hybrid `/api/recall` endpoint uses `pgvector`
> (`vector(384)`) when the extension is present on PostgreSQL.  When it is absent
> (e.g. vanilla `postgres:16` or local SQLite dev), the migration falls back to
> `TEXT` (JSON-encoded floats) automatically — hybrid search still works, just
> without the native ANN index.  Install the extension with
> `CREATE EXTENSION vector;` to enable the fast path.

> **Sandbox (Linux only):** The skill execution sandbox (`app/sandbox/`) requires
> [firejail](https://firejail.wordpress.com/) or [bubblewrap](https://github.com/containers/bubblewrap)
> — both are **Linux-only** tools.  On macOS the sandbox raises
> `SandboxBackendUnavailable` immediately (fail-loud by design — a silent
> pass-through would mean untrusted skill scripts run with unrestricted host
> access).  Sandbox tests are automatically skipped on macOS via the
> `sandbox_linux_only` pytest marker.  See
> [`docs/security/sandbox.md`](docs/security/sandbox.md) for the full platform
> support matrix and threat model.

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

Target: **76% line coverage on `app/` (aggregate) + 100% on critical paths.**
The aggregate gate is enforced in CI (`ci.yml`, `--cov-fail-under=76`). The
critical security paths — `app/middleware.py`, `app/authz.py`, `app/mcp/auth.py`
— are gated separately at ≥80% line coverage by `critical-path-coverage.yml`
so the auth/authz/sandbox surface can never silently regress, regardless of
where the aggregate number sits.

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
