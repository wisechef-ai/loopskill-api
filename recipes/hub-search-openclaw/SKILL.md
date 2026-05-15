---
name: hub-search-openclaw
description: >
  Search the OpenClaw plugin registry (via npm) to discover published plugins
  before authoring a new one. Invoke when asked "is there an existing skill for
  X on OpenClaw?", "check OpenClaw before authoring", or "search the OpenClaw
  registry for Y". The method shells out to `npm search --json --searchlimit=20
  <query>` and filters results to packages whose name starts with `@openclaw/`
  or contains `openclaw-plugin`.  An optional preview path via the gitlawb HTTP
  API (gitlawb.com/api/, alpha) is documented but not required. Output is
  canonical JSON: {hub, query, results[{name, description, version,
  install_command, source_url, license, match_score}], elapsed_ms, errors}.
  Complements hub-search-hermes for exhaustive pre-authoring discovery.
tier: cook
category: discovery
license: Apache-2.0
tags: [hub-search, discovery, openclaw, npm]
related_skills: [hub-search-claude-code, hub-search-codex, plan-for-goal]
os_supported: [linux, macos, windows]
---

# Hub Search — OpenClaw

## When to use

Run this skill before authoring any new OpenClaw plugin or skill to confirm
nothing equivalent already exists in the npm registry. Specific trigger phrases:

- "Is there an existing skill for X on OpenClaw?"
- "Check OpenClaw before authoring a plugin for Y"
- "Search the OpenClaw registry for Z"
- "What OpenClaw plugins handle image captioning?"
- "Find an openclaw-plugin for web scraping"
- "Look for an @openclaw package that does PDF summarization"

## NOT for

- Searching skills already installed locally → use `hub-search-hermes` instead
- Searching the Claude Code or Codex marketplaces → use `hub-search-claude-code`
  or `hub-search-codex` instead
- Installing discovered packages — this skill only searches; invoke `npm install`
  or `npx` in a separate step after confirming the match is correct
- Verifying that a plugin works correctly — registry metadata is self-reported
  by package authors; source review is a separate step

## Method

Requires npm and network access to the npm registry.

1. **Accept the query** — a free-text string from the caller (e.g., `"pdf summarize"`).

2. **Run npm search** — shell out to the npm CLI with JSON output and a
   result cap of 20:

   ```bash
   npm search --json --searchlimit=20 "openclaw pdf summarize"
   ```

   Prepend `openclaw` to the query if the caller omitted it, so the registry
   returns relevant packages. Capture stdout; treat a non-zero exit code as an
   error to surface in the `errors` field.

3. **Filter by namespace** — from the JSON array returned by npm, keep only
   entries where:
   - `name` starts with `@openclaw/`, OR
   - `name` contains `openclaw-plugin`

   Packages that happen to appear in the results but belong to other namespaces
   are not automatically relevant — the npm `--searchlimit` surface includes
   false positives.

4. **Score each result** — tokenize both the original user query and the
   candidate's `name + description + keywords` (space-joined) on `[\W_]+`.
   Compute:
   `match_score = |query_tokens ∩ candidate_tokens| / max(|query_tokens|, 1)`

5. **Optionally query gitlawb (preview)** —   the gitlawb HTTP API (`gitlawb.com/api/`, endpoint path `/search`) exposes a
   plugin search endpoint (v0.1.0, alpha).
   This path is **optional** and should be skipped by default because the API is
   pre-stable and carries no SLA. When enabled, merge gitlawb results (after the
   same namespace filter) into the unified `results` array before deduplicating
   on `name`. Cite the trust score from the gitlawb response when present.

6. **Rank and trim** — sort descending by `match_score`; keep the top 10.
   Entries with `match_score == 0.0` are dropped.

7. **Emit canonical JSON** — see Output schema below.

## Output schema

```json
{
  "hub": "openclaw",
  "query": "<user query>",
  "results": [
    {
      "name": "<skill name or package id>",
      "description": "<one-line description>",
      "version": "<semver or null>",
      "install_command": "<one-line install command or null>",
      "source_url": "<URL or null>",
      "license": "<spdx id or null>",
      "match_score": 0.0
    }
  ],
  "elapsed_ms": 0,
  "errors": []
}
```

Field notes:
- `install_command` — typically `npx -y <name>` for OpenClaw plugins.
- `source_url` — the `links.repository` or `links.npm` field from the npm
  response, or `null` if absent.
- `match_score` — 0.0–1.0; scores below 0.1 are typically noise.
- `errors` — list of error strings (e.g., npm CLI error messages, network
  timeouts).

## Example invocation

```bash
python3 - <<'PY'
import json, re, subprocess, time

def tokenize(s):
    return set(re.split(r'[\W_]+', (s or '').lower()))

query = "pdf summarize"
qt = tokenize(query)
npm_query = f"openclaw {query}"
t0 = time.monotonic()

try:
    proc = subprocess.run(
        ['npm', 'search', '--json', '--searchlimit=20', npm_query],
        capture_output=True, text=True, timeout=30
    )
    raw = json.loads(proc.stdout or '[]')
except Exception as exc:
    raw, errors = [], [str(exc)]
else:
    errors = [proc.stderr.strip()] if proc.returncode != 0 and proc.stderr.strip() else []

results = []
for pkg in raw:
    name = pkg.get('name', '')
    if not (name.startswith('@openclaw/') or 'openclaw-plugin' in name):
        continue
    candidate = ' '.join(filter(None, [
        name,
        pkg.get('description', ''),
        ' '.join(pkg.get('keywords') or []),
    ]))
    ct = tokenize(candidate)
    score = len(qt & ct) / max(len(qt), 1)
    if score == 0.0:
        continue
    links = pkg.get('links') or {}
    results.append({
        'name': name,
        'description': (pkg.get('description') or '')[:120],
        'version': pkg.get('version') or None,
        'install_command': f'npx -y {name}',
        'source_url': links.get('repository') or links.get('npm') or None,
        'license': pkg.get('license') or None,
        'match_score': round(score, 4),
    })

elapsed_ms = int((time.monotonic() - t0) * 1000)
results.sort(key=lambda r: r['match_score'], reverse=True)
print(json.dumps({
    'hub': 'openclaw',
    'query': query,
    'results': results[:10],
    'elapsed_ms': elapsed_ms,
    'errors': errors,
}, indent=2))
PY
```

Example output:

```json
{
  "hub": "openclaw",
  "query": "pdf summarize",
  "results": [
    {
      "name": "@openclaw/pdf-summarizer",
      "description": "Summarizes PDF documents via structured extraction.",
      "version": "0.3.1",
      "install_command": "npx -y @openclaw/pdf-summarizer",
      "source_url": "https://github.com/openclaw/pdf-summarizer",
      "license": "MIT",
      "match_score": 0.5
    }
  ],
  "elapsed_ms": 412,
  "errors": []
}
```

## Pitfalls

1. **npm anonymous rate limit** — the npm registry allows approximately 10,000
   search requests per hour per IP for unauthenticated clients. In CI or shared
   environments with many agents running concurrent searches, this limit can be
   hit. Back off with exponential delay and surface the 429 status in `errors`
   rather than retrying silently.

2. **Namespace filter is not automatic** — `npm search` returns results from
   the entire registry. Packages that appear in the result set but do not start
   with `@openclaw/` or contain `openclaw-plugin` are false positives from npm's
   full-text index; they must be explicitly discarded by the filter in step 3.
   Do not surface them in `results`.

3. **gitlawb API is alpha (v0.1.0)** — the gitlawb endpoint at
   `gitlawb.com/api/` is pre-stable and carries no uptime or schema
   SLA. When it is enabled, surface the   `trust_score` field from each gitlawb
   result alongside the match_score, and note to the caller that these scores
   are self-reported by an alpha service (`gitlawb.com/api/`, v0.1.0). Default behavior should skip gitlawb
   entirely.

4. **npm result counts include deprecated entries** — the total package count
   returned by npm search (`total` field in the metadata) includes deprecated
   and unmaintained packages. Do not cite this number as the authoritative count
   of available OpenClaw plugins. Check the `deprecated` field in each result
   and exclude or flag deprecated entries in the output.

## Verification

```bash
# Smoke-test: confirm npm is available and can reach the registry
npm search --json --searchlimit=5 "openclaw" 2>&1 | python3 -c "
import json, sys
data = json.load(sys.stdin)
print(f'npm search returned {len(data)} results')
oc = [r for r in data if r.get('name','').startswith('@openclaw/') or 'openclaw-plugin' in r.get('name','')]
print(f'Filtered to {len(oc)} @openclaw/ or openclaw-plugin packages')
"
```

## Related skills

- [[hub-search-hermes]] — scan locally installed skills in the Hermes hub
- [[hub-search-claude-code]] — scan the Claude Code extension marketplace
- [[hub-search-codex]] — scan the Codex plugin index
- [[plan-for-goal]] — author a phased plan-doc once discovery confirms no duplicate exists
