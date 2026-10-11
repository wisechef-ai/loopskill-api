# Validation record — setup paths

What has actually been executed for the install paths in
[AGENT_SETUP.md](../AGENT_SETUP.md) and the README, when, and how — and,
just as important, what has **not**. If a path is not listed under
"Verified paths", treat it as unverified, whatever other docs say.

Environment for every entry below: Linux x86_64, Python 3.14.7, git 2.43.0,
Docker 29.8.1 with Compose v5.5.1, non-interactive `bash -c` (the way coding
agents run commands). The repo was cloned **from the local
`docs/agent-setup-runbook` branch**, not from GitHub, so the branch under
review is what got tested (application and CLI code are unchanged from
`main` @ `82c6119`; the branch only adds docs).

---

## Verified paths

### 1. CLI install into a project venv — 2026-10-11

- **What:** `loopskill` 0.3.0 installs from `./cli` into `./.venv` with no
  other dependencies.
- **How:** fresh clone into an empty directory, then the AGENT_SETUP.md
  Install block.
- **Command:** `python3 -m venv .venv && ./.venv/bin/pip install ./cli && ./.venv/bin/loopskill --version`
- **Result:** `Successfully installed loopskill-0.3.0`, then `loopskill 0.3.0`.

### 2. CLI drift demo (README "60 seconds" transcript) — 2026-10-11

- **What:** `import` + `diff` reproduce the README transcript exactly;
  `diff` exits `1` on drift.
- **How:** the AGENT_SETUP.md Verify block was extracted from the file and run
  verbatim with `bash -c`; stdout compared to the file's Expected block.
- **Command:** the "CLI: the drift demo" block in AGENT_SETUP.md (README
  transcript with `./.venv/bin/loopskill` called by path).
- **Result:** byte-for-byte match, `exit=1`.

### 3. CLI in-sync case — 2026-10-11

- **What:** a fresh snapshot diffed against the live directory reports no
  drift and exits `0`.
- **How / command:** the `machine-b` block in AGENT_SETUP.md, extracted and
  run verbatim.
- **Result:** byte-for-byte match with the Expected block, `exit=0`.

### 4. CLI test suite — 2026-10-11

- **Command:** `./.venv/bin/pip install -q pytest && ./.venv/bin/python -m pytest -q cli/tests`
- **Result:** `42 passed, 2 warnings`.

### 5. `import` against a real home directory (read-only) — 2026-10-11

- **What:** the "next command" handed to the user works on real skill dirs.
- **Command:** `./.venv/bin/loopskill import -o <scratch>/real-skills.lock.json`
- **Result:** wrote a lockfile covering 3 clients; the lockfile was then
  deleted. No skill directory was modified.

### 6. Self-hosted registry via Docker Compose (zero-config SQLite) — 2026-10-11

- **What:** the stack builds, boots, migrates, seeds, answers `healthz`, and
  runs a seeded loop with the boot-banner dev key.
- **How:** run under an isolated Compose project name so nothing else on the
  host was touched, then torn down with only that project's volume and image.
- **Commands:**
  - `docker compose -p ls-coldrun up -d --build`
  - `for i in $(seq 1 40); do curl -sf localhost:8200/api/healthz && break; sleep 3; done`
  - `KEY=$(docker compose -p ls-coldrun logs api | sed -n 's/.*Dev API key: *\([^ ]*\).*/\1/p' | head -1)`
  - `curl -s -X POST localhost:8200/api/loops/hello-world-loop/run -H "x-api-key: $KEY"`
  - `docker compose -p ls-coldrun down -v --rmi local`
- **Result:** `{"status":"ok","version":"0.9.64","db":"ok",...}`;
  boot log `starter catalog: {'bundles_created': 2, 'loops_created': 10, 'personalities_created': 2}`;
  `GET /api/loops` returned 10 loops; loop run returned
  `"mode":"verify","confinement":"bounded","passed":true,"exit_code":0`.
  With an empty key the same call returned HTTP 401
  `{"detail":"authentication required to run a verifier"}`.
  `healthz` first answered on poll 4 (run 1) and poll 7 (run 2), 3 s apart.

### 7. README guard tests still pass with the new README section — 2026-10-11

- **Command:** `python -m pytest -q --noconftest tests/test_readme_claims.py tests/test_bundles0811_readme_transcript_executable.py tests/test_bundles0811_readme_install_pep668.py`
- **Result:** `10 passed`.

---

## Cold-run log

**Run 1** (README commands as written, before AGENT_SETUP.md existed):

- Install succeeded.
- `alias loopskill=./.venv/bin/loopskill` followed by `loopskill --version`
  **failed** in a non-interactive shell: bash does not expand aliases there, so
  a stale user-level `loopskill` script elsewhere on `PATH` ran instead and
  crashed with `ModuleNotFoundError: No module named 'loopskill'`.
- With the binary called by path, the transcript matched the README exactly.
- README says "run `loopskill diff` again with nothing changed and exit code
  is `0`". Re-running the *same* diff against the old lockfile still exits
  `1`; exit `0` needs a fresh snapshot. Not a bug, but an agent following the
  sentence literally would think it failed.
- Docker Compose path (run as `-p ls-coldrun`) built, booted and ran the loop
  as the README describes; the only addition needed was polling `healthz`.

**Fixes folded into AGENT_SETUP.md:** call `./.venv/bin/loopskill` by path in
self-contained blocks; preflight `command -v loopskill` to surface a stale
binary; an explicit fresh-snapshot step for the in-sync case; poll `healthz`
instead of assuming readiness; read the dev key into a variable; troubleshooting
entries for each of the above.

**Run 2** (fresh clone of the branch, following only AGENT_SETUP.md): every
Preflight, Install and Verify block ran as written and matched its Expected
output (paths 1–6 above). Preflight correctly flagged the stale `loopskill`
on `PATH`. Two runbook details were corrected from run-2 observations: the
`healthz` wait time and the exact 401 body for an empty key.

---

## Not yet validated

- **The paste-prompt itself inside Claude Code / Codex / Cursor.** The runbook
  commands were executed directly in non-interactive bash; no coding agent was
  handed the README prompt end to end.
- **Clone from GitHub.** Both runs cloned the local branch (same code as
  `main` plus these docs).
- **Other platforms and Pythons:** macOS, Windows/WSL, and Python 3.9–3.13
  (only 3.14.7 was run). The `>=3.9` floor comes from `cli/pyproject.toml`, not
  from a run.
- **The "no `venv` module" preflight branch** — not reproduced.
- **Cold Docker build on an empty cache.** Build took ~30 s (run 1) and ~2.5 s
  (run 2) with warm layer caches; first-time build time on a fresh machine is
  unmeasured.
- **Troubleshooting entries taken from code, not reproduced:** port 8200
  already bound, `/data is not writable`, leftover `/tmp/loopskill-demo`.
- **`docker-compose.prod.yml`** (Postgres + Redis) and the manual venv server
  path (`pip install -r requirements.txt`, `alembic upgrade head`, `uvicorn`).
- **Everything after boot in [SELF_HOST.md](SELF_HOST.md):** GitHub OAuth
  sign-in, owner keys, bundles, fleets, reconcile, scheduled loops.
- **CLI network/write commands:** `pull`, `apply --write`, `sync`.
- **MCP endpoint** `/api/mcp/http` — only seen in the boot banner, not called.
- **Kernel-sandbox confinement** (firejail/bubblewrap): only `bounded` mode was
  observed (the stock image ships neither).
- **Full repository test suite** — only `cli/tests` and the three README guard
  tests listed above were run.
