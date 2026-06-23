---
name: hub-search-codex
description: >
  preview — Codex CLI plugin API not yet programmatically queryable; filesystem-only
  fallback ships today. Scanner skill for LLM agents that need to discover locally
  installed Codex skills before duplicating effort or authoring a new capability.
  Walks ~/.codex/skills/**/SKILL.md as the only reliable discovery path. If the codex
  CLI is on PATH and supports --list-plugins --json (probed at runtime; non-zero exit
  tolerated), its output augments the filesystem results. When the CLI API is absent or
  returns a non-zero exit, the gap is reported via the errors[] field rather than
  treated as a fatal failure. Emits the same standard JSON envelope used by the other
  three hub-search skills so agents can union results across all hubs.
tier: pro
category: discovery
license: Apache-2.0
tags: [hub-search, discovery, codex, plugin]
related_skills: [hub-search-hermes, hub-search-openclaw, plan-for-goal]
os_supported: [linux, macos, windows]
---

# Hub Search — Codex (preview)

Scanner skill — **preview status**. Codex CLI plugin API is not yet programmatically queryable; the filesystem walk of `~/.codex/skills/` is the only reliable path today. Invoke before authoring or installing a Codex skill to check whether a matching capability already exists.

## When to use

- Before authoring a new Codex skill — confirm no equivalent already exists locally under `~/.codex/skills/`
- Building a capability inventory across all four hubs (Hermes, OpenClaw, Claude Code, Codex)
- Validating that a skill installed via the Codex CLI is discoverable on disk
- During multi-hub sweeps where partial results are acceptable

## NOT for

- Production pipelines that require complete, authoritative plugin enumeration — the Codex CLI API gap means this skill may miss programmatically-registered plugins; mark any output `(preview)` in reports to callers
- Real-time streaming search — snapshot scan only
- MCP server discovery — use hub-search-hermes
- OpenClaw or Claude Code plugin discovery — use hub-search-openclaw or hub-search-claude-code

## Method

**Step 1 — Filesystem walk (always runs)**

```
~/.codex/skills/**/SKILL.md
```

For each discovered `SKILL.md`, parse YAML frontmatter to extract `name`, `description`, `version`, and `license`. Compute a keyword `match_score` (0.0–1.0) using frontmatter `name`, `description`, and `tags`. If the directory does not exist, return `results: []` with no error entry — absence is normal on a fresh Codex install.

**Step 2 — CLI probe (conditional)**

If `codex` is on PATH, attempt:

```bash
codex --list-plugins --json
```

Tolerate non-zero exit. If the command succeeds and emits valid JSON, merge the returned plugin records into `results[]`, deduplicating by `name`. If the command fails (exit non-zero, flag unrecognised, or output is not valid JSON), append the following entry to `errors[]` and continue with filesystem-only results:

```json
{"hub": "codex", "reason": "cli_api_unavailable", "detail": "<short description of failure>"}
```

If `codex` is not on PATH at all, skip step 2 with no error entry.

**Step 3 — npm community search (best-effort)**

npm community plugins for Codex are sparse. A search for `codex-plugin-*` is best-effort:

```bash
npm search codex-plugin-* --json --searchlimit 50
```

Filter by tarball presence of `SKILL.md` or `codexManifest` key in `package.json`. On rate-limit or network failure, append to `errors[]` and continue. Mark any npm-sourced results with `"source": "npm"` in a metadata field so callers can distinguish them from locally-installed skills.

## Output schema

Standard JSON envelope shared with the other hub-search skills:

```json
{
  "hub": "codex",
  "query": "<user query>",
  "results": [
    {
      "name": "",
      "description": "",
      "version": null,
      "install_command": null,
      "source_url": null,
      "license": null,
      "match_score": 0.0
    }
  ],
  "elapsed_ms": 0,
  "errors": []
}
```

When the Codex CLI API gap is encountered, `errors[]` must contain:

```json
{"hub": "codex", "reason": "cli_api_unavailable", "detail": "codex --list-plugins --json exited 1: unknown flag"}
```

- `hub` — always `"codex"` for this skill
- `results` — ordered by `match_score` descending; may be empty on fresh installs
- `install_command` — `null` for locally-installed skills; `"npm install <pkg>"` for npm hits
- `errors[]` — non-fatal; partial results still returned when populated

## Known limitations

- **Codex CLI plugin API gap.** The `codex --list-plugins --json` interface does not exist in the current upstream CLI. Until it ships, the filesystem walk is the only reliable enumeration path. This is the defining constraint of this skill's preview status.
- **`~/.codex/skills/` may not exist on fresh installs.** This is normal; return empty results, not an error.
- **npm community ecosystem is sparse.** Few `codex-plugin-*` packages exist at this time. Expect low or zero npm results; this is an accurate reflection of the ecosystem, not a search failure.
- **Skills installed via the Codex CLI into paths outside `~/.codex/skills/` are not discovered.** Until the CLI API is available, only skills placed in the canonical filesystem path are visible.
- **Preview status applies to human and agent consumers alike.** Output and documentation should always carry `(preview)` so consumers do not assume parity with the other three hubs.

## Example invocation

Agent prompt:
```
Search for skills related to "code review" on the Codex hub.
```

Expected tool call:
```json
{
  "skill": "hub-search-codex",
  "args": { "query": "code review" }
}
```

Expected output (abbreviated, CLI API absent):
```json
{
  "hub": "codex",
  "query": "code review",
  "results": [],
  "elapsed_ms": 38,
  "errors": [
    {
      "hub": "codex",
      "reason": "cli_api_unavailable",
      "detail": "codex not found on PATH; filesystem-only results returned"
    }
  ]
}
```

Note the empty `results[]` — correct on a fresh install with no locally-placed skills. The `errors[]` entry documents the CLI gap without marking the overall response as a failure.

## Pitfalls

1. **CLI API gap is the primary constraint.** `codex --list-plugins --json` is not implemented in the current upstream Codex CLI. Do not block on a non-zero exit or missing flag — probe, tolerate failure, record in `errors[]`, and proceed with filesystem results. Never surface this as a fatal error to the caller.

2. **`~/.codex/skills/` may not exist.** On a fresh Codex install (or on machines that have Codex installed but no skills yet), the directory is absent. Return `results: []` with an empty `errors[]`. Do not treat a missing directory as an error state.

3. **npm community plugins are sparse.** The `codex-plugin-*` npm namespace is lightly populated. Zero npm results is the common case, not an anomaly. Do not inflate errors or warnings when npm returns no matches.

4. **Mark all output `(preview)` in reports.** Downstream agents and human readers must not interpret Codex hub results as authoritative or complete. When surfacing Codex results alongside other hubs, label the Codex section explicitly as `(preview)` so consumers understand the coverage gap relative to the other three hubs.

5. **npm search 429 — handle gracefully.** On rate-limit responses from the npm registry, append `{"source": "npm", "reason": "rate_limited", "detail": "HTTP 429"}` to `errors[]` and return whatever filesystem results were collected. Do not retry within the same invocation.

6. **Skills outside the canonical path are invisible.** If a user has installed Codex skills into a custom path not under `~/.codex/skills/`, this skill will not find them until the CLI API ships and exposes a plugin enumeration interface. Document this limitation in any inventory report generated from Codex hub results.

## Verification

```bash
# 1. Check whether the Codex skills directory exists
ls ~/.codex/skills/ 2>/dev/null || echo "no codex skills dir — normal on fresh install"

# 2. Probe the CLI (expect non-zero or 'unknown flag' — that is the documented gap)
codex --list-plugins --json 2>&1 || echo "cli api unavailable — expected"

# 3. Invoke the skill with a broad query via your agent
# Expected: valid JSON with hub="codex"; results may be empty; errors[] may contain
# cli_api_unavailable entry — both are correct behaviour for this preview skill
```

## Related skills

- [[hub-search-hermes]] — discovers skills registered in the Hermes plugin registry
- [[hub-search-openclaw]] — discovers OpenClaw plugins
- [[plan-for-goal]] — author a structured plan-doc before installing or building new skills
