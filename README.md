# Recipes API

**Recipes API** — skill marketplace backend for [recipes.wisechef.ai](https://recipes.wisechef.ai).

Mirrors LarryBrain's API surface (`x-api-key`, 60 req/min, `mode=files`) plus carousel + telemetry + WiseChef cross-sell.

Sister repo: [github.com/wisechef-ai/recipes-skill](https://github.com/wisechef-ai/recipes-skill) (public meta-skill).

---

## Overview

FastAPI-based backend powering the Recipes skill marketplace. It provides:

- **Skill search & discovery** — full-text search across the skill catalog
- **Signed tarball downloads** — `mode=files` compatible with LarryBrain's Alexa API surface
- **Carousel** — featured/trending skill content per day
- **Telemetry** — anonymous install and usage event tracking
- **WiseChef cross-sell** — CTA + demo request funnel embedded in the marketplace
- **x-api-key auth** — `rec_` prefixed keys, 60 req/min rate limit
- **15 SQLAlchemy models** on a dedicated PostgreSQL database

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
python -m venv venv
source venv/bin/activate
pip install fastapi uvicorn sqlalchemy pydantic pydantic-settings pydantic[email] psycopg2-binary itsdangerous
cp .env.example .env  # fill in DB URL + API key
uvicorn app.main:app --reload --port 8201
```

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
