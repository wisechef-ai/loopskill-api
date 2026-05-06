# Phase K — Skill-Seeker (cross-vendor probe)

Adds a 9th MCP tool, `recipes_seeker`, that probes the local machine for
vendor-installed skills, parses each `SKILL.md` frontmatter, and diffs the
result against the public catalog so an agent can recommend upgrades or
surface skills it has never heard of.

**READ-ONLY** on every vendor directory — no mutation, no shelling out, no
network calls.

## Supported vendor paths

| Vendor   | Linux                                  | macOS                                                    | Windows                              |
|----------|----------------------------------------|----------------------------------------------------------|--------------------------------------|
| Claude   | `~/.claude/skills/`                    | `~/Library/Application Support/Claude/skills/`           | `%APPDATA%/Claude/skills/`           |
| Codex    | `~/.codex/skills/`                     | `~/Library/Application Support/Codex/skills/`            | `%APPDATA%/Codex/skills/`            |
| Hermes   | `~/.hermes/skills/`                    | `~/Library/Application Support/Hermes/skills/`           | `%APPDATA%/Hermes/skills/`           |
| OpenCode | `~/.opencode/skills/`                  | `~/Library/Application Support/OpenCode/skills/`         | `%APPDATA%/OpenCode/skills/`         |

On Linux, `XDG_CONFIG_HOME` overrides the per-vendor home defaults
(`$XDG_CONFIG_HOME/<vendor>/skills/`). On Windows, `%APPDATA%` falls back to
`~/AppData/Roaming` if unset.

## Tool contract

`recipes_seeker(db) -> dict` — no input args.

```jsonc
{
  "vendors": {
    "claude": [
      {"vendor": "claude", "name": "alpha", "version": "1.0.0",
       "path": "/home/u/.claude/skills/alpha", "description": "demo skill"}
    ]
  },
  "recommendations": [
    {"vendor": "claude", "slug": "alpha",
     "installed_version": "1.0.0", "catalog_version": "2.0.0",
     "reason": "newer"},
    {"vendor": "claude", "slug": "beta",
     "installed_version": "0.5.0", "catalog_version": null,
     "reason": "missing"}
  ],
  "unsupported_paths": ["codex", "hermes", "opencode"]
}
```

Recommendation reasons:

- `newer` — the catalog has a higher semver than what's installed.
- `better-quality` — same version on disk, but the catalog row's
  `rating_avg` ≥ 4.5, suggesting an upgrade is still worth surfacing.
- `missing` — the installed skill has no row in the public catalog.

Equal-version skills with no quality signal are silently dropped — they
don't need any action.

## Behavior under failure

- Vendor directories that don't exist are listed in `unsupported_paths` and
  do not raise.
- `SKILL.md` files with malformed YAML, missing `name`, or unreadable
  permissions log a warning and are skipped — no crash.
- Versions that fail `packaging.version.Version` parsing fall back to
  lexicographic compare (vendor authors are creative).

## Dropped scope (R11)

- **tutorial-ingest** — Phase K v2 explicitly excludes any tutorial-ingest
  pipeline. Local skill-diff only. The marketplace surfaces curated tutorial
  content elsewhere; the seeker does not push or pull tutorial drafts.

## Non-negotiables honored

- READ-ONLY on every vendor directory (uses `Path.rglob` + `read_text`).
- Cross-platform path resolution via `sys.platform` + `XDG_CONFIG_HOME` /
  `%APPDATA%` env vars.
- Tests use `tmp_path` exclusively; no real `~/.claude/skills/` is touched.

## Test coverage

- `tests/test_seeker_paths.py` (6 tests) — Linux / macOS / Windows /
  XDG_CONFIG_HOME / APPDATA fallback / unknown-platform fallback.
- `tests/test_seeker_diff.py` (10 tests) — scan_vendor parses, malformed
  files are skipped, diff covers newer / equal / better-quality / missing /
  catalog-without-versions / non-semver-strings.
- `tests/test_seeker_mcp.py` (3 tests) — round-trip via `recipes_seeker`
  and via `call_tool_sync("recipes_seeker", ...)`, plus the all-paths-
  unsupported case.

`tests/test_mcp_server.py` updated: `_tool_definitions()` now lists 9 tools
(Phase A's 8 plus `recipes_seeker`).
