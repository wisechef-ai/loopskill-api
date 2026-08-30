# loopskill — skill portability, offline by construction

You already have skills installed across Claude, Hermes, Codex, and maybe
Cursor. They drift. `loopskill diff` shows you exactly how, in one command,
with no account and no network.

```
$ loopskill import -o machine-a.lock.json
loopskill import: wrote machine-a.lock.json (615 skill(s) across 3 client(s))

# ...on a second machine, or later on this one...
$ loopskill diff machine-a.lock.json -
loopskill diff: machine-a.lock.json  vs  <this machine>

[claude] DRIFT DETECTED
  - only in machine-a.lock.json: recipes
  ~ changed:          agent-reach
  (1 unchanged)
[codex] in sync (30 skill(s))
[cursor] in sync (0 skill(s))
[hermes] in sync (582 skill(s))

DRIFT FOUND
```

That's the whole pitch. Two machines, one command, drift visible in the
time it took to read this paragraph.

## What this is (and isn't)

`loopskill` is a **skill-portability tool for skills you already have**. It
works on your existing `~/.claude/skills`, `~/.hermes/skills`,
`~/.codex/skills`, `~/.cursor/skills` directories, whatever put them there.

It is **not a client for the LoopSkill registry**. [LoopSkill](https://app.loopskill.io)
is one *optional* backend for `pull`/`apply` — you can point `--api-base`
at any registry that serves the same well-known bundle-index shape, or skip
`pull`/`apply` entirely and only ever use `import`/`diff`.

`import` and `diff` never make a network call. Not "try not to" — the
network-capable code (`urllib`) lives in exactly one module (`loopskill.pull`)
that `import`/`diff` never import, and a test breaks `socket.socket` for the
duration of the offline commands to prove it structurally rather than by
promise. See `cli/tests/test_loopskill_cli.py::test_import_and_diff_make_zero_network_calls`
and `test_offline_modules_never_import_network_capable_stdlib`.

## Install

```
pip install loopskill
```

One command, no clone, no account. (Or from this repo: `pip install
./cli` for an editable/dev install.)

## Commands

### `loopskill import`

Discover skills already installed across every known client, by checking
well-known PATHs (`~/.claude/skills`, `~/.hermes/skills`, `~/.codex/skills`,
`~/.cursor/skills`) — never by asking you. A client that was never
installed on this machine (e.g. no Cursor) is a totally normal state:
it shows up with `installed: false, skill_count: 0`, not an error.

Emits a lockfile: sorted, versioned, checksummed JSON. Two machines that
have never talked to each other, or to us, can `diff` their lockfiles with
plain `diff -u` and get a meaningful result.

```
loopskill import                    # print lockfile to stdout
loopskill import -o mine.lock.json  # write to a file
```

### `loopskill diff`

Compare two lockfiles, or one lockfile against a live scan of the current
machine:

```
loopskill diff a.lock.json b.lock.json   # two saved snapshots
loopskill diff a.lock.json -             # a.lock.json vs THIS machine, live
loopskill diff a.lock.json               # same as above ('-' is the default)
```

Exit code is `1` if any client shows drift, `0` if everything matches — CI
and cron-friendly.

### `loopskill pull` (network, opt-in)

Fetch a public bundle's skills via the auth-free well-known index (the same
path `install.sh` uses — zero API key required for free-tier skills):

```
loopskill pull loopskill-essentials -o pulled.json
loopskill pull my-bundle --api-base https://your-registry.example.com
```

### `loopskill apply` (local disk only — never remote execution)

Converge a local directory to a pulled bundle. **Dry-run by default** —
prints exactly what it would do; nothing is written until you pass
`--write`. Idempotent: run it twice and the second run reports everything
as `up-to-date` with zero writes.

```
loopskill apply loopskill-essentials --dest ~/.claude/skills            # dry-run
loopskill apply loopskill-essentials --dest ~/.claude/skills --write    # actually write
```

`apply` only downloads bytes and writes files to your local disk. It never
executes anything it downloads — LoopSkill (or any backend you point it at)
is a control plane here, never a runner.

### `loopskill sync` (network — writes by default)

Pull a bundle's current skills and converge `--dest` to match, in one
command. This is the CLI counterpart to the server-side `loopskill_sync` MCP
tool: **it writes by default** (`--dry-run` is opt-in) — the one place this
CLI's default differs from `apply`'s dry-run-unless-`--write` convention.
Otherwise identical mechanics to `apply` (same idempotency guarantee, same
locked-skill skip, same local-disk-only execution boundary):

```
loopskill sync loopskill-essentials --dest ~/.claude/skills             # writes immediately
loopskill sync loopskill-essentials --dest ~/.claude/skills --dry-run   # preview only
```

## Lockfile format

```json
{
  "lockfile_version": 1,
  "clients": {
    "claude": {
      "root": "/home/you/.claude/skills",
      "installed": true,
      "skill_count": 2,
      "skills": [
        {"id": "agent-reach", "sha256": "...", "size_bytes": 5342,
         "name": "agent-reach", "description": "..."}
      ]
    }
  }
}
```

Deliberately excludes timestamps and hostnames — only the fields that make
two *logically identical* machines diff as identical are in the file.
`lockfile_version` is bumped on any breaking shape change so `diff` refuses
to silently misread an old-format file instead of comparing incompatible
schemas.

## Development

```
cd cli
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
pip install pytest
pytest -q tests/test_loopskill_cli.py
```
