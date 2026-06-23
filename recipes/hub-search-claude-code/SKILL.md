---
name: hub-search-claude-code
description: >
  Scanner skill for LLM agents that need to discover locally-installed Claude Code
  skills and community npm plugins before installing a duplicate or authoring a new
  skill from scratch. Walks ~/.claude/skills/**/SKILL.md (default skills directory)
  and the XDG variant at ~/.config/claude/skills/, then queries the npm registry for
  packages in the claude-plugin-* namespace. If the Hermes CLI is on PATH, augments
  results with hermes plugins search <q>. Emits a standard JSON envelope compatible
  with hub-search-hermes and hub-search-openclaw so agents can union results across
  all four hubs. Missing skills directories are treated as empty results, not errors.
tier: pro
category: discovery
license: Apache-2.0
tags: [hub-search, discovery, claude-code, plugin]
related_skills: [hub-search-hermes, hub-search-openclaw, plan-for-goal]
os_supported: [linux, macos, windows]
---

# Hub Search — Claude Code

Scanner skill. Invoke before installing or authoring a Claude Code skill to check whether a matching capability already exists locally or in the community npm registry.

## When to use

- Before authoring a new skill — confirm no equivalent already exists locally
- Before installing a `claude-plugin-*` npm package — verify it is not already present
- Building a capability inventory of the current machine's Claude Code install
- During multi-hub discovery sweeps combined with hub-search-hermes and hub-search-openclaw

## NOT for

- Searching Anthropic's official Skills directory — that catalog has no public programmatic API yet; browse it at anthropic.com manually
- Real-time streaming search — this skill performs a point-in-time snapshot scan
- MCP server discovery — use hub-search-hermes for Hermes-registered MCP skills
- OpenClaw plugin discovery — use hub-search-openclaw

## Method

**Step 1 — Local filesystem walk**

Probe both the standard and XDG-compliant paths:

```
~/.claude/skills/**/SKILL.md          # primary location
~/.config/claude/skills/**/SKILL.md   # XDG fallback
```

For each discovered `SKILL.md`, parse YAML frontmatter to extract `name`, `description`, `version`, and `license`. Compute a keyword `match_score` (0.0–1.0) against the query string using the frontmatter `name`, `description`, and `tags` fields. If neither directory exists, return `results: []` with no error entry — absence is a normal state on fresh installs.

**Step 2 — npm registry search**

Query the npm registry for the `claude-plugin-*` namespace:

```bash
npm search claude-plugin-* --json --searchlimit 100
```

For each candidate package, fetch its tarball and check for the presence of a top-level `SKILL.md` or a `skillManifest` key in `package.json`. Packages lacking either are not Claude Code plugins — discard them to avoid polluting results.

**Step 3 — Optional Hermes augmentation**

If `hermes` is on PATH:

```bash
hermes plugins search "<q>"
```

Merge returned results into `results[]`, deduplicating by `name`. If `hermes` is absent, skip this step with no error entry.

**Execution order**: steps run sequentially; a failure in step 2 or 3 appends to `errors[]` and does not abort the overall response.

## Output schema

Standard JSON envelope shared with hub-search-hermes and hub-search-openclaw:

```json
{
  "hub": "claude-code",
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

- `hub` — always `"claude-code"` for this skill
- `results` — ordered by `match_score` descending
- `install_command` — `null` for locally-installed skills; `"npm install <pkg>"` for npm hits
- `source_url` — populated from frontmatter `source_url` or npm `repository.url` when available
- `errors[]` — non-fatal issues (npm 429, parse failure, etc.); skill still returns partial results

## Known limitations

- Anthropic's official Skills directory has no public programmatic API. npm is the only community registry queried by this skill.
- npm `claude-plugin-*` search may return false positives — packages that adopt the namespace prefix without shipping a skill manifest. Filtering by tarball content reduces noise but adds latency (~200 ms/package).
- Locally-installed skills outside `~/.claude/` and `~/.config/claude/` are not found unless symlinked into one of the probed paths.
- `match_score` is keyword-based (token overlap), not semantic. Short or generic queries may produce poor rankings.

## Example invocation

Agent prompt:
```
Search for skills related to "image captioning" on the Claude Code hub.
```

Expected tool call:
```json
{
  "skill": "hub-search-claude-code",
  "args": { "query": "image captioning" }
}
```

Expected output (abbreviated):
```json
{
  "hub": "claude-code",
  "query": "image captioning",
  "results": [
    {
      "name": "claude-plugin-vision-caption",
      "description": "Generates alt-text and captions for images via Claude vision.",
      "version": "1.2.0",
      "install_command": "npm install claude-plugin-vision-caption",
      "source_url": "https://github.com/example/claude-plugin-vision-caption",
      "license": "MIT",
      "match_score": 0.87
    }
  ],
  "elapsed_ms": 412,
  "errors": []
}
```

## Pitfalls

1. **Missing `~/.claude/skills/` is normal, not an error.** On a fresh install or a machine where skills are managed globally, this directory may not exist. Return empty `results[]` with no entry in `errors[]`; do not surface a file-not-found condition to the caller.

2. **Probe both standard and XDG paths.** Some users place skills under `~/.config/claude/skills/` (XDG Base Dir spec). Searching only `~/.claude/skills/` misses these. Check both and merge results. If the same `name` appears under both paths, prefer the entry from the primary path and deduplicate.

3. **Not every `claude-plugin-*` npm package is a genuine plugin.** Filter by tarball content: accept only packages with a top-level `SKILL.md` or a `skillManifest` key in `package.json`. Packages that merely use the namespace prefix for unrelated tooling pollute results if included.

4. **npm search rate limits.** The public npm search endpoint throttles aggressive clients. On a 429 response, append `{"source": "npm", "reason": "rate_limited", "detail": "HTTP 429"}` to `errors[]` and return whatever local results were already collected. Do not retry in the same invocation.

5. **Hermes CLI is optional augmentation only.** If `hermes` is not on PATH, skip step 3 entirely — no entry in `errors[]`. Some environments intentionally omit Hermes; its absence is not a degraded state for this skill.

6. **Apply a match_score floor at the caller, not here.** Callers that merge across hubs should apply a consistent score floor (e.g. 0.1) to filter low-confidence results. This skill does not apply a floor internally so callers control precision/recall trade-offs.

## Verification

```bash
# 1. Confirm whether local skills directory exists (absent is expected on fresh installs)
ls ~/.claude/skills/ 2>/dev/null || echo "no local skills dir — expected on fresh install"
ls ~/.config/claude/skills/ 2>/dev/null || echo "no XDG skills dir"

# 2. Verify npm search is reachable and returns JSON
npm search claude-plugin-test --json --searchlimit 1 2>&1 | head -5

# 3. Invoke the skill with a broad query via your agent
# Expected: valid JSON with hub="claude-code", results array, errors array
# results may be empty on a fresh machine — that is correct behaviour
```

## Related skills

- [[hub-search-hermes]] — discovers skills registered in the Hermes plugin registry
- [[hub-search-openclaw]] — discovers OpenClaw plugins
- [[plan-for-goal]] — author a structured plan-doc before installing or building new skills
