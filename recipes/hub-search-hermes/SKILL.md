---
name: hub-search-hermes
description: >
  Scan the local Hermes skill hub (~/.hermes/skills/) to discover installed
  skills before authoring a new one. Invoke when asked "is there an existing
  skill for X?", "check Hermes before authoring", or "search local skills for
  Y". The method walks ~/.hermes/skills/**/SKILL.md, parses YAML frontmatter
  (mirroring the parse_skill_md approach in scripts/harvest_cookbook.py), and
  fuzzy-matches the query against name, description, and tags via token overlap
  scoring. Output is canonical JSON: {hub, query, results[{name, description,
  version, install_command, source_url, license, match_score}], elapsed_ms,
  errors}. Pure filesystem scan — no network calls required. Complements
  hub-search-openclaw and hub-search-claude-code for exhaustive pre-authoring
  discovery.
tier: cook
category: discovery
license: Apache-2.0
tags: [hub-search, discovery, hermes, local-skills]
related_skills: [hub-search-claude-code, hub-search-codex, plan-for-goal]
os_supported: [linux, macos, windows]
---

# Hub Search — Hermes

## When to use

Run this skill before authoring any new skill to confirm nothing equivalent
already exists locally. Specific trigger phrases:

- "Is there an existing skill for X?"
- "Check Hermes before authoring a skill for Y"
- "Search local skills for Z"
- "What installed skills handle PDF summarization?"
- "List skills matching 'web scraping' in Hermes"
- "Do I already have a skill that does image captioning?"

## NOT for

- Searching the npm registry or any remote registry → use `hub-search-openclaw`
  or `hub-search-claude-code` instead
- Skills installed outside `~/.hermes/skills/` (project-local or system-wide
  skill directories that are not under that path)
- Verifying that a skill works correctly — this reads frontmatter only, not
  tests or implementation code
- Full catalog audit, scoring, or CSV export → use `scripts/harvest_cookbook.py`
  directly for that

## Method

Pure filesystem scan. No network access needed.

1. **Accept the query** — a free-text string from the caller (e.g., `"pdf summarize"`).

2. **Discover skill files** — walk `~/.hermes/skills/` for all `SKILL.md`
   files. Cap depth at 4 directory levels. Skip any symlink whose resolved
   real path falls outside the canonical `~/.hermes/skills/` tree.

   ```bash
   python3 -c "
   import pathlib
   root = pathlib.Path('~/.hermes/skills').expanduser()
   found = [p for p in root.rglob('SKILL.md') if not p.is_symlink()]
   print(f'Found {len(found)} skill files')
   "
   ```

3. **Parse YAML frontmatter** — mimic `parse_skill_md` in
   `scripts/harvest_cookbook.py`: use `python-frontmatter`
   (`pip install python-frontmatter`) when available; otherwise slice the
   `---`-delimited block and call `yaml.safe_load` (requires PyYAML), or use
   the pure-regex fallback shown in step 4. Skip files that raise parse
   exceptions and record the path in `errors`.

   ```python
   import re

   def parse_frontmatter_regex(text):
       """Stdlib-only fallback — no PyYAML needed."""
       m = re.match(r'^---\s*\n(.*?)\n---\s*\n', text, re.DOTALL)
       if not m:
           return {}
       meta = {}
       for line in m.group(1).splitlines():
           kv = re.match(r'^([\w-]+):\s*(.*)', line)
           if kv:
               meta[kv.group(1)] = kv.group(2).strip().strip('"\'')
       return meta
   ```

4. **Build candidate text** — concatenate `name`, `description`, and `tags`
   (space-joined list) for each skill into one searchable string.

5. **Tokenize and score** — split both query and candidate on `[\W_]+`
   (non-word characters and underscores); compute:
   `match_score = |query_tokens ∩ candidate_tokens| / max(|query_tokens|, 1)`
   Range: 0.0–1.0.

6. **Rank and trim** — discard entries where `match_score == 0.0`, sort
   descending, keep the top 10 results (configurable by the caller).

7. **Emit canonical JSON** — see Output schema below.

## Output schema

```json
{
  "hub": "hermes",
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
- `install_command` — `null` for local skills already on disk.
- `source_url` — file URI of the skill directory (`file://~/.hermes/skills/<slug>`), or `null`.
- `match_score` — 0.0–1.0; scores below 0.1 are typically noise.
- `errors` — list of path strings for files that could not be read or parsed.

## Example invocation

```bash
python3 - <<'PY'
import json, pathlib, re, time

def tokenize(s):
    return set(re.split(r'[\W_]+', (s or '').lower()))

def parse_frontmatter(text):
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n', text, re.DOTALL)
    if not m:
        return {}
    meta = {}
    for line in m.group(1).splitlines():
        kv = re.match(r'^([\w-]+):\s*(.*)', line)
        if kv:
            meta[kv.group(1)] = kv.group(2).strip().strip('"\'')
    return meta

query = "pdf summarize"
qt = tokenize(query)
root = pathlib.Path('~/.hermes/skills').expanduser()
results, errors = [], []
t0 = time.monotonic()

for skill_md in root.rglob('SKILL.md'):
    if skill_md.is_symlink():
        continue
    try:
        meta = parse_frontmatter(skill_md.read_text())
    except OSError as exc:
        errors.append(f'{skill_md}: {exc}')
        continue
    candidate = ' '.join(filter(None, [
        meta.get('name', ''),
        meta.get('description', ''),
        meta.get('tags', ''),
    ]))
    ct = tokenize(candidate)
    score = len(qt & ct) / max(len(qt), 1)
    if score > 0:
        results.append({
            'name': meta.get('name', skill_md.parent.name),
            'description': meta.get('description', '')[:120],
            'version': meta.get('version') or None,
            'install_command': None,
            'source_url': None,
            'license': meta.get('license') or None,
            'match_score': round(score, 4),
        })

elapsed_ms = int((time.monotonic() - t0) * 1000)
results.sort(key=lambda r: r['match_score'], reverse=True)
print(json.dumps({
    'hub': 'hermes',
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
  "hub": "hermes",
  "query": "pdf summarize",
  "results": [
    {
      "name": "pdf-summarizer",
      "description": "Extracts text from a PDF and produces a structured summary.",
      "version": "1.2.0",
      "install_command": null,
      "source_url": null,
      "license": "Apache-2.0",
      "match_score": 0.5
    }
  ],
  "elapsed_ms": 18,
  "errors": []
}
```

## Pitfalls

1. **Permission errors on skill files** — `~/.hermes/skills/` may contain
   subdirectories owned by other users or set mode 700. Wrap every `read_text()`
   in a `try/except OSError` and append the path to `errors`; do not abort
   the entire scan on a single failure.

2. **Malformed YAML frontmatter** — a skill with an unterminated string,
   tab-indented block, or non-UTF-8 bytes will cause the parser to raise.
   Catch all parse exceptions per file, skip the entry, and record the path
   in `errors`.

3. **Performance on large installs (>500 skills)** — a naïve `rglob('SKILL.md')`
   can take several seconds. Cap the walk at depth 4, skip `node_modules` and
   `.git` subdirectories explicitly, and consider caching the parsed frontmatter
   index in a temp file keyed by directory mtime.

4. **Symlink escape** — a symlink inside `~/.hermes/skills/` may resolve to a
   path outside the directory (e.g., pointing to a system path). Resolve each
   path with `.resolve()` and verify the result starts with the canonical
   resolved root before reading. Skip and log anything that escapes.

## Verification

```bash
# Smoke-test: count SKILL.md files and confirm frontmatter is parseable
python3 -c "
import pathlib, re

root = pathlib.Path('~/.hermes/skills').expanduser()
skill_files = [p for p in root.rglob('SKILL.md') if not p.is_symlink()]
print(f'Skill files found: {len(skill_files)}')

ok, fail = 0, []
for p in skill_files:
    try:
        text = p.read_text()
        if re.match(r'^---', text):
            ok += 1
        else:
            fail.append(str(p) + ' (no frontmatter)')
    except OSError as exc:
        fail.append(f'{p}: {exc}')

print(f'Parseable frontmatter: {ok}/{len(skill_files)}')
if fail:
    print('Issues (first 5):', fail[:5])
"
```

## Related skills

- [[hub-search-claude-code]] — scan the Claude Code extension marketplace
- [[hub-search-codex]] — scan the Codex plugin index
- [[plan-for-goal]] — author a phased plan-doc once discovery confirms no duplicate exists
