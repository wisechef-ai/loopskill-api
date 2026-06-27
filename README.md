# LoopSkill

**Open-core, self-hostable registry for AI agents — and the only one where loops actually RUN.**
Browse, install, and deploy **skills · bundles · loops · personalities**. Zero signup to self-host.

<p>
  <a href="https://github.com/wisechef-ai/loopskill-api/actions/workflows/ci.yml"><img src="https://github.com/wisechef-ai/loopskill-api/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-MPL--2.0-blue.svg" alt="License: MPL-2.0"></a>
  <a href="https://github.com/wisechef-ai/loopskill-api/stargazers"><img src="https://img.shields.io/github/stars/wisechef-ai/loopskill-api?style=flat" alt="Stars"></a>
  <a href="https://loopskill.io"><img src="https://img.shields.io/badge/home-loopskill.io-7c5cff.svg" alt="loopskill.io"></a>
  <img src="https://img.shields.io/badge/MCP-native-00e5c0.svg" alt="MCP native">
</p>

> **Spotify for AI agents.** A registry your agents pull runnable artifacts from — skills,
> bundles, loops, and personalities. The novel part: a **loop** carries its safety envelope
> (max turns, tool allow-list, verification, stopping criteria), and the registry **runs that
> verification and returns an objective pass/fail**. "Did the loop succeed?" is measured, not asserted.

---

## Run it locally in 60 seconds

**One-liner (Docker):**
```sh
curl -fsSL loopskill.io/install | sh
```

**Or clone + compose:**
```sh
git clone https://github.com/wisechef-ai/loopskill-api
cd loopskill-api && docker compose up
```

No `.env`, no secrets, no Postgres required. The default stack uses SQLite, replays the real
alembic migrations, and seeds a starter catalog (skills + 9 loops + personalities) automatically.
Your dev API key is printed on first boot.

**Then RUN a loop** — the runner is live (no LLM needed for verify-mode):
```sh
# use the dev API key printed in the boot banner
curl -X POST localhost:8200/api/loops/hello-world-loop/run \
  -H "x-api-key: ${LOOPSKILL_KEY}" \
# → {"passed": true, "confinement": "bounded", "duration_seconds": 0.03, ...}
```

That's the whole wow: a fresh registry that doesn't just *list* a loop — it *executes* the loop's
success check under enforced bounds and hands you a verdict.

Browse the hosted registry: **[loopskill.io](https://loopskill.io)** · full guide: [docs/SELF_HOST.md](docs/SELF_HOST.md)

---

## What's in the box

Four first-class, pullable artifact types:

| Artifact | What it is |
|----------|------------|
| **skill** | One capability — a `SKILL.md` + optional scripts/refs. |
| **bundle** | A curated set you deploy + sync to a whole fleet at once. |
| **loop** | A safety-bounded autonomous agent loop — the part no other registry has. |
| **personality** | A deployable persona (system prompt + config). |

### Loops are the bet

People hand-roll agentic loops and they spin out — runaway turns, surprise bills. A LoopSkill loop
ships as a **contract**, stored as structured columns (not free text) so the registry can validate it
on publish and enforce it at run time:

- `success_condition` — the goal, in plain language
- `verification_script` — a command that **objectively** checks success
- `max_turns` — a hard ceiling on autonomous turns
- `tool_allowlist` — deny-by-default tool access
- `stopping_criteria` — success / failure / budget stops

And then the registry **runs it**. `POST /api/loops/{slug}/run` executes the loop's
`verification_script` under enforced bounds — POSIX rlimits (CPU, memory, file size), a hard
wall-clock timeout, an isolated workspace, and a **scrubbed, secret-free environment** so the script
never sees the server's own credentials — and returns an objective `passed: true|false` plus the
`confinement` level it achieved (`sandboxed` when a firejail/bwrap backend is present, else `bounded`
POSIX rlimits, which run everywhere including the zero-config Docker image).

The starter catalog ships **9 vetted loops** — `hello-world`, `test-green` (TDD), `lint-clean`,
`secret-scan`, `doc-coverage`, `changelog-from-commits`, `json-schema-validate`, and more. Each
verification is **non-vacuous**: it passes on good input and *fails* on bad (the secret-scan loop
catches a planted AWS key; the doc-coverage loop catches a missing docstring).

> Verify-mode (running the verification under bounds) is shipped. The autonomous **agent-driving**
> layer — bringing your own LLM to drive a loop's `system_prompt` within the bounds — is a clean
> pluggable seam on the roadmap (`mode=agent` currently returns `501`). The contract is enforced; the
> driver is yours.

---

## Why open-core

The **whole registry is the OSS product** (MPL-2.0). Self-host it anywhere — `docker compose up` is the
complete experience, not a teaser, and nothing phones home. The hosted plan is "don't run it
yourself," never a feature gate. Same posture as n8n / PostHog / Supabase.

---

## Core API surface

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/healthz` | DB health check |
| `GET` | `/api/skills/search` | Full-text + vector hybrid skill search |
| `GET` | `/api/skills/{slug}` | Skill detail + versions |
| `GET` | `/api/bundles/discover` | Browse public bundles |
| `GET` | `/api/loops` | List loops (with their safety contracts) |
| `GET` | `/api/loops/{slug}` | Loop detail — contract, run count, rating |
| `POST` | `/api/loops` | Publish a loop (validates the contract) |
| `POST` | `/api/loops/{slug}/run` | **Run the loop's verification → objective pass/fail** |
| `POST` | `/api/loops/{slug}/rate` | Rate a loop 1–5 (social-proof signal) |
| `GET` | `/api/personalities` | List deployable personalities |

It's **MCP-native**: agents (Claude Code, Cursor, anything speaking MCP) discover and install over the
protocol. There's also a signed-URL tarball path for direct fetch.

---

## Architecture

FastAPI + SQLAlchemy. The same alembic migration chain runs on **SQLite (self-host)** and
**Postgres (hosted)** — no `create_all` drift; the SQLite boot replays the real migrations, so what you
self-host is what production runs.

```
app/
├── main.py                  FastAPI app factory + lifespan
├── config.py                Settings + boot-time secrets gate
├── auth_ctx.py              AuthContext (scope, user_id, tier, …) — single source of truth
├── authz.py                 Authorization predicates (can_install, can_write_bundle, …)
├── middleware.py            APIKeyMiddleware → request.state.auth_ctx
├── loop_routes.py           /api/loops/* — registry + the RUNNER (/run) + /rate
├── loop_runner.py           LoopRunner — tiered confinement (sandboxed / bounded)
├── loop_runner_support.py   Pure helpers: env scrub, rlimits, bounded read, path safety
├── bundle_routes.py         /api/bundles/* — discover, compose, install, sync
├── skill_routes.py          /api/skills/* — search, detail, install
├── mcp/                     MCP server + tools (install, sync, …)
├── sandbox/                 Kernel sandbox backend (firejail / bwrap, Linux-only)
└── …                        (auth, checkout, creator, publisher, admin)
```

### Auth flow

```
APIKeyMiddleware.dispatch()
  └─ validate_key(db, x-api-key)
       └─ request.state.auth_ctx = AuthContext(scope, user_id, tier, …)
            └─ REST routes / MCP tools / runner call authz.can_*() predicates
```

API keys are `rec_`-prefixed and passed in the `x-api-key` header. Most read endpoints (search, detail, discover) are
unauthenticated; install, publish, run, and rate require a key.

---

## Develop

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pre-commit install                       # ruff, bandit, mypy --strict, actionlint, yamllint

pytest -q                                # fast run
pytest -n auto --cov=app --cov-fail-under=80   # the CI gate

alembic upgrade head                     # apply migrations before first start (non-SQLite)
uvicorn app.main:app --reload --port 8201
```

> **Sandbox (Linux only):** the kernel sandbox (`app/sandbox/`) needs
> [firejail](https://firejail.wordpress.com/) or [bubblewrap](https://github.com/containers/bubblewrap).
> Where neither is functional (macOS, hardened containers), the loop runner falls back to **bounded**
> mode — POSIX rlimits + scrubbed env + isolated workspace — so loops still run; the response declares
> which `confinement` level it achieved. Multi-tenant operators set `WR_LOOP_RUN_REQUIRE_SANDBOX=true`
> to refuse bounded-mode execution and require a real kernel sandbox.

Contributor guide for AI agents: [AGENTS.md](./AGENTS.md).

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `WR_DATABASE_URL` | `sqlite:////data/loopskill.db` (compose) | DB connection string — SQLite self-host or Postgres |
| `WR_API_KEY` | `rec_dev_…` | Master / dev API key (printed on first boot) |
| `WR_SIGNING_SECRET` | `wr-tarball-…` | HMAC signing secret for download tokens |
| `WR_LOOP_RUN_REQUIRE_SANDBOX` | `false` | Refuse bounded-mode loop runs (multi-tenant safety) |
| `WR_RATE_LIMIT_PER_MINUTE` | `60` | Requests per minute per IP |
| `WR_PORT` | `8201` | Bind port |

---

## License

MPL-2.0 — see [LICENSE](./LICENSE). The whole registry is open source; we only charge for hosting it.

## Links

- **Home:** [loopskill.io](https://loopskill.io)
- **Self-host guide:** [docs/SELF_HOST.md](docs/SELF_HOST.md)
- **Contributing (AI agents):** [AGENTS.md](./AGENTS.md)
