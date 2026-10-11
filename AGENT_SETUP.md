# AGENT_SETUP.md — install, verify and hand off LoopSkill (runbook for coding agents)

You are a coding agent (Claude Code, Codex, Cursor, …) and a user asked you to
install or set up this project. Follow this file top to bottom. It is written
for you, not for contributors — contributor conventions live in
[AGENTS.md](AGENTS.md).

Rules for the whole runbook:

- **Each command block is self-contained.** Agents usually run every command in
  a fresh, non-interactive shell, so blocks re-declare what they need (`LS=…`)
  and never rely on an `alias` or an earlier `cd`. Run every block from the
  root of the clone unless the block itself `cd`s.
- **Stop and ask the user** before anything in [Safety](#safety): deleting
  data, stopping or removing containers, `sudo`, or writing into their real
  skill directories.
- Compare real output against the **Expected** blocks. If it differs, go to
  [Troubleshooting](#troubleshooting) before improvising.

Every command and expected output below was executed from a fresh clone; the
record (what, when, how, and what was *not* checked) is
[docs/VALIDATION.md](docs/VALIDATION.md).

---

## What you will end up with

**Primary path (always do this): the `loopskill` CLI** in a project-local
virtualenv at `./.venv`, proven working by a reproducible drift demo.

- `import` snapshots the skills installed for Claude / Codex / Cursor / Hermes
  into a lockfile; `diff` compares two snapshots, or a snapshot against this
  machine. Both are **offline** — no account, no network call.
- No server, no account, no API key, no Docker.

**Optional path (only if the user asks for the registry/server): a local
self-hosted registry** via Docker Compose — FastAPI on `http://localhost:8200`,
SQLite in a Docker volume, a seeded starter catalog (10 loops), and an
auto-printed **dev-only** API key.

---

## Preflight

### CLI path

```sh
python3 --version
python3 -m venv --help >/dev/null && echo venv-ok
git --version
ls -d /tmp/loopskill-demo 2>/dev/null && echo "DEMO DIR EXISTS" || echo "demo dir free"
command -v loopskill || echo "no loopskill on PATH"
```

Expected (versions may be newer):

```
Python 3.14.7
venv-ok
git version 2.43.0
demo dir free
no loopskill on PATH
```

- Python must be **3.9 or newer** (`requires-python = ">=3.9"` in
  `cli/pyproject.toml`). The CLI has zero runtime dependencies.
- No `venv-ok` → the interpreter has no `venv` module. Do not `sudo` anything;
  tell the user which package their OS needs and stop.
- `DEMO DIR EXISTS` → the demo below writes into and deletes inside
  `/tmp/loopskill-demo`. **Ask the user** before reusing it.
- A `loopskill` already on `PATH` is fine — but it is **not** the one you are
  about to install. Always call `./.venv/bin/loopskill` explicitly (see
  [Troubleshooting](#troubleshooting)).

### Docker path (only if the user wants the self-hosted registry)

```sh
docker --version
docker compose version
(ss -ltn 2>/dev/null || netstat -ltn) | grep -q ':8200 ' && echo "PORT 8200 BUSY" || echo "8200 free"
```

Expected (versions may differ; Compose v2 is required — `docker compose`, not `docker-compose`):

```
Docker version 29.8.1, build 4a63305
Docker Compose version v5.5.1
8200 free
```

`PORT 8200 BUSY` → something else owns the port. **Ask the user** what it is;
do not stop it yourself.

---

## Install

### CLI (primary)

If the user has not cloned the repo yet:

```sh
git clone https://github.com/wisechef-ai/loopskill-api && cd loopskill-api
```

Then, from the clone root:

```sh
python3 -m venv .venv && ./.venv/bin/pip install ./cli
./.venv/bin/loopskill --version
```

Expected — the last line (pip output above it varies):

```
loopskill 0.3.0
```

### Self-hosted registry (optional)

From the clone root:

```sh
docker compose up -d --build
```

The first build downloads the Python base image and installs
`requirements.txt` into the image; it took ~30 s here with a warm Docker
cache — budget several minutes cold. The container then runs migrations and
seeds the catalog before the API answers; poll `healthz` (below), do not
assume it is up the moment `up -d` returns.

---

## Verify

### CLI: the drift demo (proves `import` + `diff` work)

This is the README's 60-second demo, with the binary called by full path. It
fabricates two fake skills under `/tmp/loopskill-demo`, snapshots them, edits
one and deletes the other, then diffs.

```sh
LS=./.venv/bin/loopskill
$LS --version
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
$LS import --home /tmp/loopskill-demo/home -o /tmp/loopskill-demo/machine-a.lock.json
cat > /tmp/loopskill-demo/home/.claude/skills/agent-reach/SKILL.md <<'EOF'
---
name: agent-reach
description: Give your agent eyes on the internet.
---
Body v2 — updated tool list.
EOF
rm -rf /tmp/loopskill-demo/home/.claude/skills/recipes
$LS diff /tmp/loopskill-demo/machine-a.lock.json - --home /tmp/loopskill-demo/home
echo "exit=$?"
```

Expected — exactly:

```
loopskill 0.3.0
loopskill import: wrote /tmp/loopskill-demo/machine-a.lock.json (2 skill(s) across 1 client(s))
loopskill diff: /tmp/loopskill-demo/machine-a.lock.json  vs  <live scan>

[claude] DRIFT DETECTED
  - only in /tmp/loopskill-demo/machine-a.lock.json: recipes
  ~ changed:          agent-reach
[codex] in sync (0 skill(s))
[cursor] in sync (0 skill(s))
[hermes] in sync (0 skill(s))

DRIFT FOUND
exit=1
```

`exit=1` is the **success** signal here: drift was found, as staged. Now prove
the in-sync case with a fresh snapshot of the current state:

```sh
LS=./.venv/bin/loopskill
$LS import --home /tmp/loopskill-demo/home -o /tmp/loopskill-demo/machine-b.lock.json
$LS diff /tmp/loopskill-demo/machine-b.lock.json - --home /tmp/loopskill-demo/home
echo "exit=$?"
```

Expected — exactly:

```
loopskill import: wrote /tmp/loopskill-demo/machine-b.lock.json (1 skill(s) across 1 client(s))
loopskill diff: /tmp/loopskill-demo/machine-b.lock.json  vs  <live scan>

[claude] in sync (1 skill(s))
[codex] in sync (0 skill(s))
[cursor] in sync (0 skill(s))
[hermes] in sync (0 skill(s))

No drift — machines match.
exit=0
```

Both blocks matching = the CLI is installed and working. Optional deeper
check (installs `pytest` into the same venv):

```sh
./.venv/bin/pip install -q pytest && ./.venv/bin/python -m pytest -q cli/tests
```

Expected last line: `42 passed` (a warnings count may follow).

### Self-hosted registry (only if you ran the Docker install)

```sh
for i in $(seq 1 40); do curl -sf localhost:8200/api/healthz && break; sleep 3; done; echo
```

Expected (one line; `version` tracks the repo):

```
{"status":"ok","version":"0.9.64","db":"ok","stripe_webhook_lag_seconds":null,"stripe_last_event_at":null,"stripe_webhook_lag_label":null}
```

Then run a seeded loop. The dev key is printed in the boot banner; read it
into a variable rather than copying it around:

```sh
KEY=$(docker compose logs api 2>/dev/null | sed -n 's/.*Dev API key: *\([^ ]*\).*/\1/p' | head -1)
curl -s -X POST localhost:8200/api/loops/hello-world-loop/run -H "x-api-key: $KEY"; echo
```

Expected — a JSON object starting with these fields (`run_id` and
`duration_seconds` differ per run):

```
{"run_id":"90f6ccd76f3d","loop_slug":"hello-world-loop","mode":"verify","confinement":"bounded","passed":true,"exit_code":0,...}
```

`"passed":true` is the proof. `confinement` is `bounded` inside the stock
container (no firejail/bubblewrap there) — that is expected, not an error.

---

## What to tell the user next

Report, in this order:

1. **What is installed:** `loopskill 0.3.0` in `./.venv` of the clone, and the
   two verify results (drift demo `exit=1`, in-sync check `exit=0`). If you ran
   Docker: registry up at `http://localhost:8200` (API docs at `/docs`, MCP at
   `/api/mcp/http`), `hello-world-loop` returned `"passed":true`.
2. **The useful next command** — snapshot *their real* skills. This only reads
   their skill directories and writes one lockfile:

   ```sh
   ./.venv/bin/loopskill import -o ~/loopskill.lock.json
   ```

   Later, `./.venv/bin/loopskill diff ~/loopskill.lock.json -` shows what
   drifted since. Copy the lockfile to another machine to diff two machines.
3. **What touches the network or their files** (offer, don't run):
   `pull` fetches a public bundle (network); `apply` is a dry run unless given
   `--write`; `sync` **writes by default** (opt out with `--dry-run`). Full reference:
   [cli/README.md](cli/README.md).
4. **Cleanup they can do** (ask before you do it): `rm -rf /tmp/loopskill-demo`
   removes the demo files; `docker compose down` stops the registry.
5. **For anything beyond a local trial** (real secrets, Postgres, GitHub
   sign-in, fleets): [docs/SELF_HOST.md](docs/SELF_HOST.md). They set those
   values themselves.

---

## Troubleshooting

Only failures actually hit during the cold run, or visible in the code.

**`loopskill: command not found`, or `ModuleNotFoundError: No module named 'loopskill'`
right after a successful install.** You ran the bare name. The README's
`alias loopskill=./.venv/bin/loopskill` does nothing in a non-interactive
shell (bash does not expand aliases there), so the shell either finds nothing
or — worse — a *different* `loopskill` earlier on `PATH` (this happened in the
cold run: a stale user-level script ran and crashed). Call
`./.venv/bin/loopskill` by path, as every block here does.

**Re-running the drift `diff` still prints `DRIFT FOUND` / exits 1.** Expected:
`machine-a.lock.json` is the *old* snapshot, so it keeps disagreeing with the
edited directory. To see the in-sync case take a new snapshot (the
`machine-b` block above) and diff that.

**Demo output has extra skills or different counts.** `/tmp/loopskill-demo`
held leftovers from an earlier run. Ask the user before
`rm -rf /tmp/loopskill-demo`, then rerun the demo.

**`healthz` fails for the first few seconds after `docker compose up -d`.**
The container is still migrating and seeding.
Keep polling `healthz` (the loop above allows ~2 minutes); in the cold run it
answered on the 4th try (~10 s). Check progress with
`docker compose logs api | tail -30` — the `LoopSkill is ready!` banner
precedes the server start.

**`docker compose up` fails to bind port 8200.** `docker-compose.yml`
publishes the fixed host port `8200`; something else holds it (see Preflight).
Ask the user; do not stop the other process.

**Container exits with `[entrypoint] FATAL: /data is not writable`.**
(`entrypoint.sh`) The SQLite volume is owned by the wrong user, usually a
volume reused from an older image. The fix is `docker compose down -v`, which
**deletes the registry database** — ask the user first.

**Loop run returns an auth error instead of `"passed"`.** `KEY` is empty —
the banner was not in the logs yet. `echo ${#KEY}` should print `37`; re-read
it after `healthz` is ok.

---

## Safety

- **Ask before deleting anything.** That includes `rm -rf /tmp/loopskill-demo`
  if it existed before you started, and above all `docker compose down -v`
  (deletes the registry's SQLite volume). Plain `docker compose down` stops
  containers but keeps the data — still ask, since it stops a service.
- **Never touch containers, volumes or processes you did not create.** If a
  port is busy, report it; do not kill the owner.
- **Don't write into the user's real skill directories** (`~/.claude/skills`,
  `~/.codex/skills`, `~/.cursor/skills`, `~/.hermes/skills`) unless they ask.
  `import` and `diff` only read them; `apply --write` and `sync` (writes unless
  `--dry-run`) change them — both default to `~/.claude/skills`.
- **No `sudo`.** If a system package is missing, tell the user what is missing.
- **Secrets never go through chat.** Don't paste API keys, tokens or the
  boot-banner key into the conversation; read keys into shell variables as
  above. Production values (`WR_API_KEY`, `WR_JWT_SECRET`,
  `WR_SIGNING_SECRET`, `WR_GITHUB_CLIENT_SECRET`, `GITHUB_TOKEN`, …) are set
  **by the user** in their own `.env` (Compose reads it if present) or
  service environment — see [docs/SELF_HOST.md](docs/SELF_HOST.md). Don't
  generate, guess or commit them.
- **The dev key is public.** The zero-config stack's dev key is hardcoded in
  `scripts/bootstrap.py`, and `docker-compose.yml` publishes port 8200 on all
  interfaces. Tell the user not to run this stack on a publicly reachable host
  without setting their own `WR_API_KEY`.
