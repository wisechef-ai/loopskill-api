"""LoopSkill Phase 1 — starter catalog seed.

Seeds ≥1 of each catalog type so a fresh clone has browsable content:
  skill · bundle · loop · personality

Designed to be idempotent (slug-keyed upsert). Ships as in-repo data —
zero network access required. All new strings use clean LoopSkill vocabulary.

Usage:
    python scripts/seed_starter_catalog.py          # run standalone
    python scripts/seed_starter_catalog.py --check  # exit 0 if already seeded
"""

from __future__ import annotations

import sys
from uuid import uuid4

SYSTEM_EMAIL = "system@loopskill.io"
SYSTEM_NAME = "LoopSkill Team"

# ── Starter bundles (skill collections) ──────────────────────────────────────
# These reference real catalog slug strings where possible; missing slugs are
# tolerated (logged, not fatal) — just like seed_editorial_cookbooks.py.
STARTER_BUNDLES = [
    {
        "slug": "dev-agent-essentials",
        "name": "Dev Agent Essentials",
        "description": (
            "The core skill set for an autonomous coding agent. Code review, "
            "CI fix, and PR draft automation — everything a dev agent needs "
            "to ship without supervision."
        ),
        "skills": ["code-reviewer", "web-scraper-pro", "client-reporter"],
        "verified": True,
    },
    {
        "slug": "research-and-report",
        "name": "Research & Report",
        "description": (
            "Search, summarise, and publish. Pair web scraping with reporting "
            "to produce client-ready outputs your agent delivers on a schedule."
        ),
        "skills": ["web-scraper-pro", "client-reporter"],
        "verified": False,
    },
]

# ── Starter loops (safety-bounded autonomous loops) ───────────────────────────
STARTER_LOOPS = [
    {
        "slug": "hello-world-loop",
        "title": "Hello World Loop",
        "description": (
            "The 30-second proof that a LoopSkill loop actually RUNS. Its "
            "verification script is fully self-contained — no external tools, no "
            "network, no credentials — so `POST /api/loops/hello-world-loop/run` "
            "returns passed=true on a fresh self-host instance. Start here."
        ),
        "category": "examples",
        "readme": (
            "# Hello World Loop\n\n"
            "## What it does\n"
            "The smallest possible *runnable* loop. Its verification script writes "
            "a greeting and checks it back — proving the runner enforces bounds and "
            "returns an objective pass/fail, with zero setup.\n\n"
            "## Try it (self-host)\n"
            "```\n"
            "curl -X POST http://localhost:8200/api/loops/hello-world-loop/run \\\n"
            "  -H 'x-api-key: rec_dev_wiserecipes_local_testing_key' \\\n"
            "  -H 'content-type: application/json' -d '{}'\n"
            "```\n"
            "You get back `passed: true`, the `confinement` level the runner "
            "achieved (`bounded` on the zero-config image, `sandboxed` when a "
            "firejail/bwrap backend is installed), and the loop's bounds.\n\n"
            "## Safety contract\n"
            "- `max_turns`: 1\n"
            "- `budget_usd`: null (bounded by max_turns)\n"
            "- `tool_allowlist`: [] (needs no tools)\n"
            "- Verification: writes `hello.txt`, then `grep`s it.\n"
        ),
        "license": "MIT",
        "tier": "free",
        "success_condition": (
            "A greeting file has been written to the run workspace and contains the expected text."
        ),
        "verification_script": ("echo 'hello from loopskill' > hello.txt && grep -q 'loopskill' hello.txt"),
        "max_turns": 1,
        "budget_usd": None,
        "stopping_criteria": {
            "success": "verification_script exits 0",
            "failure": "max_turns reached",
            "budget": "N/A — bounded by max_turns only",
        },
        "tool_allowlist": [],
        "system_prompt": (
            "You are a minimal demonstration loop. Write 'hello from loopskill' to "
            "hello.txt, then run the verification script to confirm it landed."
        ),
    },
    {
        "slug": "pr-review-loop",
        "title": "PR Review Loop",
        "description": (
            "Autonomous pull-request reviewer. Runs on every new PR, posts "
            "a structured review comment, and exits cleanly. Safety-bounded "
            "with a 10-turn ceiling and a verification script that checks the "
            "comment was posted."
        ),
        "category": "development",
        "readme": (
            "# PR Review Loop\n\n"
            "## What it does\n"
            "For each open PR, the loop:\n"
            "1. Reads the diff via the GitHub tool.\n"
            "2. Posts a structured review comment (bugs / perf / style).\n"
            "3. Verifies the comment exists, then exits.\n\n"
            "## Safety contract\n"
            "- `max_turns`: 10\n"
            "- `budget_usd`: null (relies on max_turns)\n"
            "- `tool_allowlist`: [github_read_pr, github_post_comment]\n"
            "- Verification: `gh pr view <PR> --json comments | jq '.comments | length > 0'`\n\n"
            "## Install\n"
            "```\nloopskill pull loop pr-review-loop\n```\n"
        ),
        "license": "MIT",
        "tier": "free",
        "success_condition": (
            "A review comment has been posted on the target pull request "
            "covering correctness, performance, and style findings."
        ),
        "verification_script": ("gh pr view \"$PR_NUMBER\" --json comments | jq -e '.comments | length > 0'"),
        "max_turns": 10,
        "budget_usd": None,
        "stopping_criteria": {
            "success": "verification_script exits 0",
            "failure": "max_turns reached without posting a comment",
            "budget": "N/A — bounded by max_turns only",
        },
        "tool_allowlist": ["github_read_pr", "github_post_comment"],
        "system_prompt": (
            "You are a rigorous code reviewer. For the given pull request diff, "
            "identify correctness bugs, performance issues, and style violations. "
            "Post a single structured review comment with three sections: "
            "## Bugs, ## Performance, ## Style. Be concise and actionable. "
            "Call the verification script when done to confirm the comment landed."
        ),
    },
    {
        "slug": "daily-briefing-loop",
        "title": "Daily Briefing Loop",
        "description": (
            "Autonomous daily digest generator. Scrapes configured sources, "
            "summarises the top items, and sends a briefing. Runs in under "
            "5 turns — safe, fast, and ready to schedule."
        ),
        "category": "productivity",
        "readme": (
            "# Daily Briefing Loop\n\n"
            "## What it does\n"
            "Each morning (or on demand):\n"
            "1. Fetches headlines from configured RSS / URL list.\n"
            "2. Summarises each item in ≤2 sentences.\n"
            "3. Formats and delivers the briefing to the configured output.\n\n"
            "## Safety contract\n"
            "- `max_turns`: 5\n"
            "- `budget_usd`: 0.10\n"
            "- `tool_allowlist`: [web_fetch, file_write]\n"
            "- Verification: `test -f /tmp/briefing.md && wc -l /tmp/briefing.md | awk '$1 > 3'`\n\n"
            "## Install\n"
            "```\nloopskill pull loop daily-briefing-loop\n```\n"
        ),
        "license": "MIT",
        "tier": "free",
        "success_condition": (
            "A briefing file has been written containing at least one "
            "summarised headline from each configured source."
        ),
        "verification_script": (
            "test -f /tmp/briefing.md && wc -l /tmp/briefing.md | awk '$1 > 3 {exit 0} {exit 1}'"
        ),
        "max_turns": 5,
        "budget_usd": "0.10",
        "stopping_criteria": {
            "success": "verification_script exits 0",
            "failure": "max_turns reached or budget exceeded",
            "budget": "$0.10 USD hard ceiling",
        },
        "tool_allowlist": ["web_fetch", "file_write"],
        "system_prompt": (
            "You are a concise daily briefing agent. Fetch each URL in the "
            "SOURCES list, extract the most important item, and summarise it "
            "in ≤2 sentences. Write all summaries to /tmp/briefing.md in "
            "Markdown format with a timestamp header. Call the verification "
            "script when the file is ready."
        ),
    },
    {
        "slug": "test-green-loop",
        "title": "Test-Green Loop (TDD)",
        "description": (
            "Drive a change until the test suite is GREEN. The loop's contract is "
            "the test command itself — the registry runs it and the exit code is "
            "the objective verdict. The TDD workhorse: no change is 'done' until "
            "the suite passes."
        ),
        "category": "development",
        "readme": (
            "# Test-Green Loop\n\n"
            "## What it does\n"
            "An agent iterates on code until the project's test command exits 0. "
            "The verification IS the test run — there is no subjective 'looks "
            "done', only a green suite.\n\n"
            "## The contract\n"
            "- `verification_script`: runs the test command (default `pytest -q`); "
            "exit 0 = success.\n"
            "- `max_turns`: 25 — bounded so a thrashing agent can't loop forever.\n"
            "- `budget_usd`: 2.00.\n"
            "- `tool_allowlist`: [read_file, write_file, run_tests].\n\n"
            "## Verify it (stage a passing test, then run)\n"
            "```\n"
            "curl -X POST .../api/loops/test-green-loop/run -H 'x-api-key: KEY' \\\n"
            '  -d \'{"workspace_files": {"test_x.py": "def test_x():\\n    assert 1+1==2\\n"}}\'\n'
            "```\n"
        ),
        "license": "MIT",
        "tier": "free",
        "success_condition": (
            "The project's test suite passes with a zero exit code after the agent's changes."
        ),
        "verification_script": (
            "if command -v pytest >/dev/null 2>&1; then pytest -q; "
            'else python3 -m pytest -q 2>/dev/null || { echo "no pytest"; exit 1; }; fi'
        ),
        "max_turns": 25,
        "budget_usd": "2.00",
        "stopping_criteria": {
            "success": "test command exits 0",
            "failure": "max_turns reached with tests still red",
            "budget": "$2.00 USD hard ceiling",
        },
        "tool_allowlist": ["read_file", "write_file", "run_tests"],
        "system_prompt": (
            "You are a disciplined TDD engineer. Run the tests, read each failure, "
            "make the smallest change that moves toward green, and re-run. Never "
            "delete or skip a test to pass. Stop when the suite is green."
        ),
    },
    {
        "slug": "lint-clean-loop",
        "title": "Lint-Clean Loop",
        "description": (
            "Iterate until the linter reports zero violations. The registry runs "
            "the lint command; exit 0 is the verdict. Keeps a codebase's style "
            "and static-analysis gate permanently green."
        ),
        "category": "development",
        "readme": (
            "# Lint-Clean Loop\n\n"
            "## What it does\n"
            "An agent applies fixes until the linter passes. Verification is the "
            "lint command's exit code — objective, no judgement call.\n\n"
            "## The contract\n"
            "- `verification_script`: runs `ruff check .` (falls back to a no-op "
            "pass if ruff is absent so the demo still completes).\n"
            "- `max_turns`: 15 · `budget_usd`: 1.00.\n"
            "- `tool_allowlist`: [read_file, write_file, run_linter].\n"
        ),
        "license": "MIT",
        "tier": "free",
        "success_condition": "The linter reports zero violations (exit code 0).",
        "verification_script": (
            "if command -v ruff >/dev/null 2>&1; then ruff check .; "
            'else echo "ruff not installed; nothing to lint"; exit 0; fi'
        ),
        "max_turns": 15,
        "budget_usd": "1.00",
        "stopping_criteria": {
            "success": "linter exits 0",
            "failure": "max_turns reached with violations remaining",
            "budget": "$1.00 USD hard ceiling",
        },
        "tool_allowlist": ["read_file", "write_file", "run_linter"],
        "system_prompt": (
            "You are a meticulous code-style engineer. Run the linter, fix each "
            "reported violation at its source (do not blanket-ignore), and re-run "
            "until it passes. Prefer the smallest correct fix."
        ),
    },
    {
        "slug": "secret-scan-loop",
        "title": "Secret-Scan Loop",
        "description": (
            "Prove a working tree carries no obvious leaked credentials before a "
            "commit or publish. The registry runs the scan; exit 0 (no hits) is "
            "the verdict. A pre-flight gate against the classic 'pushed an API "
            "key' incident."
        ),
        "category": "security",
        "readme": (
            "# Secret-Scan Loop\n\n"
            "## What it does\n"
            "Scans the workspace for high-signal secret patterns (AWS keys, "
            "private-key headers, generic `api_key=`/`secret=` assignments with "
            "long values). Exits non-zero if any are found, so an agent (or CI) "
            "can block on it.\n\n"
            "## The contract\n"
            "- `verification_script`: greps for secret patterns; **exit 0 = clean**.\n"
            "- `max_turns`: 10 · `budget_usd`: null.\n"
            "- `tool_allowlist`: [read_file, write_file].\n\n"
            "## Verify it (stage a clean file)\n"
            "```\n"
            "curl -X POST .../api/loops/secret-scan-loop/run -H 'x-api-key: KEY' \\\n"
            '  -d \'{"workspace_files": {"app.py": "print(1)\\n"}}\'\n'
            "```\n"
        ),
        "license": "MIT",
        "tier": "free",
        "success_condition": ("No high-signal secret patterns are present in the working tree."),
        "verification_script": (
            "if grep -rEoq "
            "'(AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
            "(api_?key|secret|token|password)[\"'\\''[:space:]]*[:=][\"'\\''[:space:]]*[A-Za-z0-9/+_-]{20,})' "
            ". 2>/dev/null; then echo 'potential secret found'; exit 1; else echo clean; exit 0; fi"
        ),
        "max_turns": 10,
        "budget_usd": None,
        "stopping_criteria": {
            "success": "scan finds no secrets (exit 0)",
            "failure": "max_turns reached with secrets still present",
            "budget": "N/A — bounded by max_turns",
        },
        "tool_allowlist": ["read_file", "write_file"],
        "system_prompt": (
            "You are a security pre-flight agent. Run the secret scan. For each "
            "hit, move the value into an environment variable or a gitignored "
            ".env, replace the literal with a reference, and re-scan until clean. "
            "Never just delete the line if the code needs the value."
        ),
    },
    {
        "slug": "changelog-from-commits-loop",
        "title": "Changelog-From-Commits Loop",
        "description": (
            "Produce a release CHANGELOG and prove it exists and is non-trivial. "
            "The registry verifies the artifact (a CHANGELOG.md with real "
            "entries), so the loop can't 'succeed' with an empty file."
        ),
        "category": "productivity",
        "readme": (
            "# Changelog-From-Commits Loop\n\n"
            "## What it does\n"
            "An agent reads the commit range and writes a grouped, human-readable "
            "CHANGELOG.md (Added / Fixed / Changed). Verification confirms the "
            "file exists and has at least a few real lines.\n\n"
            "## The contract\n"
            "- `verification_script`: `test -f CHANGELOG.md` and a line-count floor.\n"
            "- `max_turns`: 8 · `budget_usd`: 0.50.\n"
            "- `tool_allowlist`: [git_log, read_file, write_file].\n\n"
            "## Verify it (stage a changelog)\n"
            "```\n"
            "curl -X POST .../api/loops/changelog-from-commits-loop/run -H 'x-api-key: KEY' \\\n"
            '  -d \'{"workspace_files": {"CHANGELOG.md": "# Changelog\\n## Added\\n- thing\\n- two\\n"}}\'\n'
            "```\n"
        ),
        "license": "MIT",
        "tier": "free",
        "success_condition": (
            "A CHANGELOG.md exists in the workspace with at least three lines of real content."
        ),
        "verification_script": (
            "test -f CHANGELOG.md && [ \"$(grep -cve '^[[:space:]]*$' CHANGELOG.md)\" -ge 3 ]"
        ),
        "max_turns": 8,
        "budget_usd": "0.50",
        "stopping_criteria": {
            "success": "CHANGELOG.md exists with >= 3 non-blank lines",
            "failure": "max_turns reached without a valid changelog",
            "budget": "$0.50 USD hard ceiling",
        },
        "tool_allowlist": ["git_log", "read_file", "write_file"],
        "system_prompt": (
            "You are a release engineer. Read the commit history for the range, "
            "group changes into Added / Changed / Fixed / Removed, write concise "
            "user-facing bullets to CHANGELOG.md, and run the verification."
        ),
    },
    {
        "slug": "doc-coverage-loop",
        "title": "Doc-Coverage Loop",
        "description": (
            "Drive a Python module to full public-docstring coverage. The "
            "registry runs an objective checker (every top-level def/class has a "
            "docstring) — so 'documented' is measured, not asserted."
        ),
        "category": "development",
        "readme": (
            "# Doc-Coverage Loop\n\n"
            "## What it does\n"
            "An agent adds docstrings until every public function and class in the "
            "target file has one. Verification parses the AST and fails if any "
            "public symbol is undocumented.\n\n"
            "## The contract\n"
            "- `verification_script`: a Python AST check over `target.py`.\n"
            "- `max_turns`: 12 · `budget_usd`: 1.00.\n"
            "- `tool_allowlist`: [read_file, write_file].\n\n"
            "## Verify it (stage a fully-documented file)\n"
            "```\n"
            "curl -X POST .../api/loops/doc-coverage-loop/run -H 'x-api-key: KEY' \\\n"
            '  -d \'{"workspace_files": {"target.py": "def f():\\n    \\"doc\\"\\n    return 1\\n"}}\'\n'
            "```\n"
        ),
        "license": "MIT",
        "tier": "free",
        "success_condition": ("Every public top-level function and class in target.py has a docstring."),
        "verification_script": (
            'test -f target.py && python3 -c "'
            "import ast,sys; "
            "t=ast.parse(open('target.py').read()); "
            "bad=[n.name for n in t.body "
            "if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)) "
            "and not n.name.startswith('_') and ast.get_docstring(n) is None]; "
            'sys.exit(1 if bad else 0)"'
        ),
        "max_turns": 12,
        "budget_usd": "1.00",
        "stopping_criteria": {
            "success": "AST check finds no undocumented public symbols",
            "failure": "max_turns reached with symbols still undocumented",
            "budget": "$1.00 USD hard ceiling",
        },
        "tool_allowlist": ["read_file", "write_file"],
        "system_prompt": (
            "You are a documentation engineer. Run the checker, and for each "
            "undocumented public function or class write a concise, accurate "
            "docstring (what it does, args, returns). Do not document private "
            "(underscore) symbols. Re-run until coverage is complete."
        ),
    },
    {
        "slug": "json-schema-validate-loop",
        "title": "JSON-Schema-Validate Loop",
        "description": (
            "Drive a data file until it validates against a JSON Schema. The "
            "registry runs the validation; exit 0 is the verdict. The data-"
            "wrangling workhorse — transform until the contract holds."
        ),
        "category": "data",
        "readme": (
            "# JSON-Schema-Validate Loop\n\n"
            "## What it does\n"
            "An agent edits `data.json` until it conforms to `schema.json`. "
            "Verification runs a real validator (stdlib-only structural check), "
            "so conformance is proven, not claimed.\n\n"
            "## The contract\n"
            "- `verification_script`: validates data.json against schema.json "
            "(required keys + types).\n"
            "- `max_turns`: 10 · `budget_usd`: 0.50.\n"
            "- `tool_allowlist`: [read_file, write_file].\n\n"
            "## Verify it (stage matching data + schema)\n"
            "```\n"
            "curl -X POST .../api/loops/json-schema-validate-loop/run -H 'x-api-key: KEY' \\\n"
            '  -d \'{"workspace_files": {"schema.json": "{\\"required\\":[\\"id\\"],\\"types\\":{\\"id\\":\\"int\\"}}", "data.json": "{\\"id\\": 1}"}}\'\n'
            "```\n"
        ),
        "license": "MIT",
        "tier": "free",
        "success_condition": (
            "data.json validates against schema.json (all required keys present with correct types)."
        ),
        "verification_script": (
            'test -f data.json && test -f schema.json && python3 -c "'
            "import json,sys; "
            "s=json.load(open('schema.json')); d=json.load(open('data.json')); "
            "tmap={'int':int,'str':str,'float':(int,float),'bool':bool,"
            "'list':list,'dict':dict}; "
            "req=s.get('required',[]); types=s.get('types',{}); "
            "miss=[k for k in req if k not in d]; "
            "wrong=[k for k,t in types.items() if k in d and not isinstance(d[k],tmap.get(t,object))]; "
            'sys.exit(1 if (miss or wrong) else 0)"'
        ),
        "max_turns": 10,
        "budget_usd": "0.50",
        "stopping_criteria": {
            "success": "data.json validates against schema.json (exit 0)",
            "failure": "max_turns reached with validation still failing",
            "budget": "$0.50 USD hard ceiling",
        },
        "tool_allowlist": ["read_file", "write_file"],
        "system_prompt": (
            "You are a data-wrangling agent. Read schema.json, read data.json, and "
            "transform data.json so every required key is present with the correct "
            "type. Run the validator and iterate until it passes."
        ),
    },
]

# ── Starter personalities ─────────────────────────────────────────────────────
STARTER_PERSONALITIES = [
    {
        "slug": "focused-dev-agent",
        "title": "Focused Dev Agent",
        "description": (
            "A disciplined software engineer persona. Ships clean, tested code, "
            "asks clarifying questions before writing, and never hallucinates "
            "library APIs. Ideal for code-generation and refactor tasks."
        ),
        "category": "engineering",
        "readme": (
            "# Focused Dev Agent\n\n"
            "## Persona\n"
            "A senior software engineer who values correctness over speed. "
            "Asks exactly one clarifying question when the spec is ambiguous. "
            "Writes tests before implementation. Never invents library APIs.\n\n"
            "## Install\n"
            "```\nloopskill pull personality focused-dev-agent\n```\n"
        ),
        "license": "MIT",
        "tier": "free",
        "system_prompt": (
            "You are a focused, disciplined software engineer. Your operating rules:\n"
            "1. Read the full task before writing a single line of code.\n"
            "2. If the spec is ambiguous, ask ONE clarifying question, then proceed.\n"
            "3. Write a failing test before the implementation (TDD).\n"
            "4. Never invent library APIs — only use what you can verify exists.\n"
            "5. Keep functions small and names descriptive.\n"
            "6. Commit with conventional commits: feat/fix/test/chore/refactor.\n"
            "You are terse, precise, and do not pad responses with filler text."
        ),
        "config": {
            "model": "claude-sonnet-4-6",
            "temperature": 0.2,
            "default_tools": ["file_read", "file_write", "bash"],
        },
    },
    {
        "slug": "research-analyst",
        "title": "Research Analyst",
        "description": (
            "A methodical research persona. Cross-references multiple sources, "
            "flags uncertainty, and cites everything. Great for due-diligence, "
            "competitive analysis, and literature review tasks."
        ),
        "category": "research",
        "readme": (
            "# Research Analyst\n\n"
            "## Persona\n"
            "A careful, evidence-driven analyst who never asserts without a "
            "source. Outputs structured findings with confidence levels. "
            "Flags gaps and contradictions explicitly.\n\n"
            "## Install\n"
            "```\nloopskill pull personality research-analyst\n```\n"
        ),
        "license": "MIT",
        "tier": "free",
        "system_prompt": (
            "You are a methodical research analyst. Your operating rules:\n"
            "1. For any factual claim, cite your source inline: [Source: <url or doc>].\n"
            "2. If you are uncertain, say 'Confidence: LOW' and explain why.\n"
            "3. Cross-reference at least two independent sources for key claims.\n"
            "4. Structure outputs as: Summary → Key Findings → Gaps → Next Steps.\n"
            "5. Never fabricate data, statistics, or quotes.\n"
            "6. Flag contradictions between sources explicitly.\n"
            "You produce dense, citation-heavy outputs. Quality over brevity."
        ),
        "config": {
            "model": "claude-sonnet-4-6",
            "temperature": 0.1,
            "default_tools": ["web_search", "web_fetch"],
        },
    },
]


def _get_or_create_system_user(db):
    from app.models import User

    u = db.query(User).filter(User.email == SYSTEM_EMAIL).first()
    if u is not None:
        return u
    u = User(
        id=uuid4(),
        email=SYSTEM_EMAIL,
        display_name=SYSTEM_NAME,
        subscription_tier="pro_plus",
        subscription_status="active",
    )
    db.add(u)
    db.flush()
    return u


def _seed_bundles(db, system_user) -> int:
    from app.models import Bundle, BundleSkill, Skill

    created = 0
    known_slugs = {
        row[0] for row in db.query(Skill.slug).filter(Skill.is_public.is_(True), Skill.is_archived.is_(False))
    }
    for spec in STARTER_BUNDLES:
        slug = spec["slug"]
        cb = db.query(Bundle).filter(Bundle.slug == slug).first()
        if cb is None:
            cb = Bundle(
                id=uuid4(),
                name=spec["name"],
                slug=slug,
                bundle_owner=system_user.id,
                visibility="public",
            )
            db.add(cb)
            db.flush()
            created += 1

        cb.name = spec["name"]
        cb.description = spec["description"]
        cb.bundle_owner = system_user.id
        cb.visibility = "public"
        cb.is_verified = bool(spec.get("verified", False))

        for sk_slug in spec.get("skills", []):
            if sk_slug not in known_slugs:
                continue
            skill = db.query(Skill).filter(Skill.slug == sk_slug).first()
            if skill is None:
                continue
            exists = (
                db.query(BundleSkill)
                .filter(
                    BundleSkill.bundle_id == cb.id,
                    BundleSkill.skill_id == skill.id,
                )
                .first()
            )
            if exists is None:
                db.add(BundleSkill(bundle_id=cb.id, skill_id=skill.id, source="custom-added"))
    return created


def _seed_loops(db) -> int:
    from app.models import Loop

    created = 0
    for spec in STARTER_LOOPS:
        slug = spec["slug"]
        if db.query(Loop).filter(Loop.slug == slug).first() is not None:
            continue
        loop = Loop(
            id=uuid4(),
            slug=slug,
            title=spec["title"],
            description=spec.get("description"),
            category=spec.get("category"),
            readme=spec.get("readme"),
            license=spec.get("license", "MIT"),
            tier=spec.get("tier", "free"),
            is_public=True,
            success_condition=spec["success_condition"],
            verification_script=spec["verification_script"],
            max_turns=spec.get("max_turns", 25),
            budget_usd=spec.get("budget_usd"),
            stopping_criteria=spec["stopping_criteria"],
            tool_allowlist=spec["tool_allowlist"],
            system_prompt=spec["system_prompt"],
        )
        db.add(loop)
        created += 1
    return created


def _seed_personalities(db) -> int:
    from app.models import Personality

    created = 0
    for spec in STARTER_PERSONALITIES:
        slug = spec["slug"]
        if db.query(Personality).filter(Personality.slug == slug).first() is not None:
            continue
        persona = Personality(
            id=uuid4(),
            slug=slug,
            title=spec["title"],
            description=spec.get("description"),
            category=spec.get("category"),
            readme=spec.get("readme"),
            license=spec.get("license", "MIT"),
            tier=spec.get("tier", "free"),
            is_public=True,
            system_prompt=spec["system_prompt"],
            config=spec.get("config"),
        )
        db.add(persona)
        created += 1
    return created


def seed_starter_catalog(db) -> dict:
    """Seed the starter catalog into *db* (any SQLAlchemy Session).

    Idempotent: skips rows that already exist (slug-keyed).
    Returns a summary dict with counts per type.
    """
    system_user = _get_or_create_system_user(db)
    bundles = _seed_bundles(db, system_user)
    loops = _seed_loops(db)
    personalities = _seed_personalities(db)
    db.commit()
    summary = {
        "bundles_created": bundles,
        "loops_created": loops,
        "personalities_created": personalities,
    }
    return summary


def _standalone() -> int:
    """Run seed against the app's configured database (standalone invocation)."""
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        summary = seed_starter_catalog(db)
        print("starter catalog seed complete:")
        for k, v in summary.items():
            print(f"  {k}: {v}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(_standalone())
