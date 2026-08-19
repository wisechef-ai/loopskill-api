# LoopSkill

**A local CLI for skills you already have, plus a self-hostable registry to
pull more from.** Start with the CLI — it needs no account and makes no
network call for `import`/`diff`.

<p>
  <a href="https://github.com/wisechef-ai/loopskill-api/actions/workflows/ci.yml"><img src="https://github.com/wisechef-ai/loopskill-api/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-MPL--2.0-blue.svg" alt="License: MPL-2.0"></a>
  <a href="https://github.com/wisechef-ai/loopskill-api/stargazers"><img src="https://img.shields.io/github/stars/wisechef-ai/loopskill-api?style=flat" alt="Stars"></a>
  <a href="https://loopskill.io"><img src="https://img.shields.io/badge/home-loopskill.io-7c5cff.svg" alt="loopskill.io"></a>
  <img src="https://img.shields.io/badge/MCP-native-00e5c0.svg" alt="MCP native">
</p>

---

## 60 seconds: see skill drift, reproducibly

This is a literal, self-contained transcript. Paste every command below
into a clean shell — it fabricates two fake "skill" installs, edits one
and deletes the other to simulate drift, then diffs. You will get
*exactly* this output; nothing here depends on skills you already have
installed.

```sh
git clone https://github.com/wisechef-ai/loopskill-api && cd loopskill-api
python3 -m venv .venv && ./.venv/bin/pip install ./cli
alias loopskill=./.venv/bin/loopskill   # or add ./.venv/bin to PATH
loopskill --version

# --- fabricate a "machine" with two skills, take a snapshot ---
mkdir -p /tmp/loopskill-demo/home/.claude/skills/agent-reach
mkdir -p /tmp/loopskill-demo/home/.claude/skills/recipes
cat > /tmp/loopskill-demo/home/.claude/skills/agent-reach/SKILL.md <<'EOF'
---
name: agent-reach
description: Give your agent eyes on the internet.
---
Body v1.
EOF
cat > /tmp/loopskill-demo/home/.claude/skills/recipes/SKILL.md <<'EOF'
---
name: recipes
description: Recipe search skill.
---
Body.
EOF
loopskill import --home /tmp/loopskill-demo/home -o /tmp/loopskill-demo/machine-a.lock.json

# --- simulate drift: edit one skill, delete the other ---
cat > /tmp/loopskill-demo/home/.claude/skills/agent-reach/SKILL.md <<'EOF'
---
name: agent-reach
description: Give your agent eyes on the internet.
---
Body v2 — updated tool list.
EOF
rm -rf /tmp/loopskill-demo/home/.claude/skills/recipes

loopskill diff /tmp/loopskill-demo/machine-a.lock.json - --home /tmp/loopskill-demo/home
```

```
loopskill 0.2.0
loopskill import: wrote /tmp/loopskill-demo/machine-a.lock.json (2 skill(s) across 1 client(s))
loopskill diff: /tmp/loopskill-demo/machine-a.lock.json  vs  <live scan>

[claude] DRIFT DETECTED
  - only in /tmp/loopskill-demo/machine-a.lock.json: recipes
  ~ changed:          agent-reach
[codex] in sync (0 skill(s))
[cursor] in sync (0 skill(s))
[hermes] in sync (0 skill(s))

DRIFT FOUND
```

Exit code is `1` — drift found, script- and CI-friendly. Run `loopskill
diff` again with nothing changed and exit code is `0`. That's the whole
pitch: two snapshots, one command, drift visible in the time it took to
read this paragraph — on the skills you actually have, not a demo, once
you drop `--home`.

`import` and `diff` make **zero network calls** — this isn't a promise in
a docstring, it's a structural guarantee: the network-capable code lives
in exactly one module (`loopskill.pull`) that `import`/`diff` never
import, and
`cli/tests/test_loopskill_cli.py::test_import_and_diff_make_zero_network_calls`
proves it by breaking `socket.socket` for the duration of those commands.

Full CLI reference, lockfile format, and `pull`/`apply` (the two commands
that DO touch the network, opt-in): **[cli/README.md](cli/README.md)**.

---

## What this repo actually is

Two things, and the CLI is the one to start with:

1. **`cli/`** — a local, offline-by-construction tool for the skills you
   already have on disk. No account, no server, no LoopSkill dependency
   for `import`/`diff`. Point it at any registry that serves the same
   well-known bundle-index shape for `pull`/`apply`, or never call those
   commands at all.
2. **`app/`** — a self-hostable FastAPI registry (this repo) that the CLI's
   `pull`/`apply` can optionally talk to, and that also serves a browsable
   catalog at [loopskill.io](https://loopskill.io). The registry is **not**
   the reason to start here — the CLI working on your own machine, before
   you've made an account, is.

## Loops: two limits stated up front

The registry also serves **10 vetted loops** (`scripts/seed_starter_catalog.py`).
`POST /api/loops/{slug}/run` is synchronous and works anywhere. Putting a loop on
a *fleet member* so it fires on a schedule is a second path with two constraints
worth knowing before you build on it:

1. **A loop reports nothing unless its own prompt says to.** Telemetry exists
   only because the loop's prompt calls `scripts/loopskill-emit-run.sh`. Nothing
   else observes a fire — not the scheduler, not the server. Omit that line and
   the loop runs forever while every dashboard shows zero. This is the reason
   `loop_runs` sat at 1 for a year.
2. **Cron materialization is Hermes-only.** `app/loop_apply.py` writes the Hermes
   scheduler's `~/.hermes/cron/jobs.json`, and nothing else speaks that format
   yet. On Codex, Claude or OpenCode hosts `scripts/install-loop-apply.sh`
   refuses rather than installing a cron that can never converge. The skill path
   is cross-vendor; the scheduled-loop path is not.

Both are covered end to end in [docs/SELF_HOST.md](docs/SELF_HOST.md#running-loops).

## Self-host the registry (optional, for `pull`/`apply` against your own instance)

```sh
git clone https://github.com/wisechef-ai/loopskill-api
cd loopskill-api && docker compose up
```

Zero-config: SQLite, auto-generated dev secrets, a seeded starter catalog.
Your dev API key is printed on first boot. Full guide, including the
Postgres/production path: **[docs/SELF_HOST.md](docs/SELF_HOST.md)**.

**Then run a loop** — the runner is live (no LLM needed for verify-mode):

```sh
# the zero-config Docker Compose stack always boots with this dev key
# (override via WR_API_KEY in production) — same value the boot banner prints
curl -X POST localhost:8200/api/loops/hello-world-loop/run \
  -H "x-api-key: rec_dev_wiserecipes_local_testing_key"
# → {"passed": true, "confinement": "bounded", "duration_seconds": 0.03, ...}
```

A fresh registry that doesn't just *list* a loop — it *executes* the
loop's success check under enforced bounds and hands you a verdict.

---

## What's actually in this codebase (and the honest answer to "why so big")

291 app Python files, 69,809 lines of app code, 107 Alembic migrations,
413 test files, **2 GitHub stars, 0 forks** (measured 2026-08-19 via `gh
repo view wisechef-ai/loopskill-api --json stargazerCount,forkCount`).

That ratio is real and it isn't a good one. [Issue
#68](https://github.com/wisechef-ai/loopskill-api/issues/68) asked about
it; the honest answer — including why the codebase grew from a working
recipe-search product's battle-tested auth/Stripe/sandbox stack rather
than from a blank registry, and the concrete cuts committed as a result —
is in
[docs/decisions/2026-08-11-bundles0811-p4-issue-68-codebase-size.md](docs/decisions/2026-08-11-bundles0811-p4-issue-68-codebase-size.md).
Every number above is checked against a live filesystem measurement by
`tests/test_readme_claims.py` on every run — it fails the build if this
paragraph drifts from reality the way #68's original numbers did.

---

## Core API surface (self-hosted registry)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/healthz` | DB health check |
| `GET` | `/api/skills/search` | Full-text + vector hybrid skill search |
| `GET` | `/api/skills/{slug}` | Skill detail + versions |
| `GET` | `/api/bundles/discover` | Browse public bundles |
| `GET` | `/api/loops` | List loops (with their safety contracts) |
| `GET` | `/api/loops/{slug}` | Loop detail — contract, run count, rating |
| `POST` | `/api/loops` | Publish a loop (validates the contract) |
| `POST` | `/api/loops/{slug}/run` | Run the loop's verification → objective pass/fail |
| `POST` | `/api/loops/{slug}/rate` | Rate a loop 1–5 (social-proof signal) |
| `GET` | `/api/personalities` | List deployable personalities |

MCP-native: agents (Claude Code, Cursor, anything speaking MCP) discover
and install over the protocol. There's also a signed-URL tarball path for
direct fetch.

## Architecture

FastAPI + SQLAlchemy. The same alembic migration chain runs on SQLite
(self-host) and Postgres (hosted) — no `create_all` drift; the SQLite boot
replays the real migrations, so what you self-host is what production
runs. Full module layout: **[AGENTS.md](AGENTS.md)**.

### Auth flow

```
APIKeyMiddleware.dispatch()
  └─ validate_key(db, x-api-key)
       └─ request.state.auth_ctx = AuthContext(scope, user_id, tier, …)
            └─ REST routes / MCP tools / runner call authz.can_*() predicates
```

API keys are `rec_`-prefixed and passed in the `x-api-key` header. Most
read endpoints (search, detail, discover) are unauthenticated; install,
publish, run, and rate require a key.

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
> which `confinement` level it achieved. Multi-tenant fleet owners set `WR_LOOP_RUN_REQUIRE_SANDBOX=true`
> to refuse bounded-mode execution and require a real kernel sandbox.

Contributor guide for AI agents: [AGENTS.md](./AGENTS.md).

---

## Why open-core

The whole registry is the OSS product (MPL-2.0). Self-host it anywhere —
`docker compose up` is the complete experience, not a teaser, and nothing
phones home. The hosted plan is "don't run it yourself," never a feature
gate. Same posture as n8n / PostHog / Supabase.

## License

MPL-2.0 — see [LICENSE](./LICENSE). The whole registry is open source; we only charge for hosting it.

## Links

- **CLI (start here):** [cli/README.md](cli/README.md)
- **Home:** [loopskill.io](https://loopskill.io)
- **Self-host guide:** [docs/SELF_HOST.md](docs/SELF_HOST.md)
- **Issue #68 answer:** [docs/decisions/2026-08-11-bundles0811-p4-issue-68-codebase-size.md](docs/decisions/2026-08-11-bundles0811-p4-issue-68-codebase-size.md)
- **Contributing (AI agents):** [AGENTS.md](./AGENTS.md)
