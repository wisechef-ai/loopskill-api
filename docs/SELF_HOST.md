# LoopSkill — Self-Host Guide (60-second quickstart)

> **Zero signup. Zero secrets. Your own skill registry in under 60 seconds.**

---

## Option A — One-liner install (Docker)

```sh
curl -fsSL loopskill.io/install | sh
```

The script detects Docker, clones the repo if needed, runs `docker compose up -d`,
and prints the local URL + dev API key when healthy.

---

## Option B — `make dev` (for contributors / local dev)

```sh
git clone https://github.com/wisechef-ai/loopskill-api
cd loopskill-api
make dev
```

`make dev` prefers Docker if available; falls back to a local venv + SQLite if not.

---

## Option C — Manual Docker Compose

```sh
git clone https://github.com/wisechef-ai/loopskill-api
cd loopskill-api
docker compose up -d
```

The default `docker-compose.yml` uses SQLite — **no Postgres, no Redis, no .env needed.**

Watch the logs until the banner appears:

```sh
docker compose logs -f api
```

Verify healthy:

```sh
curl http://localhost:8200/api/healthz
# → {"status":"ok","db":"ok","version":"0.5.0",...}
```

---

## What you get on first boot

The container automatically runs migrations (or `create_all` for SQLite) and seeds
a starter catalog so you have browsable content immediately:

| Type | Count seeded |
|------|-------------|
| Skills | 6 |
| Bundles | 2 |
| Loops | 2 |
| Personalities | 2 |

---

## Dev API key

No OAuth required for the SQLite / local path. The default API key works out of the box:

```
rec_dev_wiserecipes_local_testing_key
```

---

## First commands

**List skills:**
```sh
curl http://localhost:8200/api/skills/search \
     -H "x-api-key: rec_dev_wiserecipes_local_testing_key"
```

**Browse the API docs:**
```
http://localhost:8200/docs
```

**Connect Claude Code (MCP):**
```sh
# Add to your claude_desktop_config.json or .claude/settings.json:
{
  "mcpServers": {
    "loopskill": {
      "type": "http",
      "url": "http://localhost:8200/api/mcp/http",
      "headers": {
        "x-api-key": "rec_dev_wiserecipes_local_testing_key"
      }
    }
  }
}
```

---

## Production deployment (Postgres + Redis)

For a production-grade stack:

```sh
cp wiserecipes-api.env.example .env
# Edit .env — set all WR_* secrets
docker compose -f docker-compose.prod.yml up -d
```

The prod compose file requires non-default secrets; the API boot-guard refuses to start
without them (preventing silent use of change-me defaults).

---

## Stopping the stack

```sh
make down
# or:
docker compose down
```

Data is persisted in the `loopskill_data` named volume. To wipe and start fresh:

```sh
docker compose down -v
```

---

## Useful targets

| Command | What it does |
|---------|-------------|
| `make dev` | Start the zero-config stack |
| `make seed` | Re-run the starter catalog seed |
| `make test` | Run the full test suite |
| `make down` | Stop the stack |
