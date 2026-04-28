# Copilot Review — Fix Contract

> Single-page wire spec for the Turn 2 fix subagents addressing Copilot's PR feedback. Each fix is scoped, justified, and has a test acceptance criterion.

## Scope

15 inline comments across 2 PRs + 1 false-positive from S3-C. **Adam's policy is "merge to main when reviews are clean" — these reviews are NOT clean. Fix all of them in this same branch before merge.**

## Subagent F-API — recipes-api fixes

### F-API-01 [HIGH SECURITY] Secrets in `wiserecipes-api.service`

**File:** `wiserecipes-api.service`

**Issue:** Service unit hardcodes API key, signing secret, DB URL inline as `Environment=` lines. Even if these are placeholders, committing them creates the risk pattern.

**Fix:** Replace the inline `Environment=` lines with a single `EnvironmentFile=/etc/wiserecipes-api.env`. Add a `wiserecipes-api.env.example` with placeholder values + a `# DO NOT COMMIT ACTUAL SECRETS` header. Add `.gitignore` entry for `wiserecipes-api.env`.

**Test:** `grep -E "WR_API_KEY=[^$]" wiserecipes-api.service` should return zero matches.

### F-API-02 [HIGH SECURITY] Path traversal in `_store_tarball`

**File:** `app/publisher_routes.py:205` (function `_store_tarball`)

**Issue:** `slug` and `semver` are interpolated into a filesystem path with no validation. A malicious skill.toml with `name = "../../../etc/something"` could write outside the skills dir.

**Fix:** Add validation at the start of the publish handler (before `_store_tarball` is called):
```python
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(-[a-zA-Z0-9.-]+)?$")
if not SLUG_RE.match(slug):
    raise HTTPException(422, detail=f"Invalid slug: {slug!r}")
if not SEMVER_RE.match(version):
    raise HTTPException(422, detail=f"Invalid version: {version!r}")
```
Also add a defense-in-depth check inside `_store_tarball`:
```python
dest_dir = (_skills_dir() / slug).resolve()
if not str(dest_dir).startswith(str(_skills_dir().resolve()) + "/"):
    raise HTTPException(422, detail="path traversal detected")
```

**Test:** Add `test_publish_path_traversal_in_slug_rejected` and `test_publish_path_traversal_in_version_rejected` — both must return 422 and not create any file.

### F-API-03 [HIGH] Creator auto-create misses `creator_id`

**File:** `app/publisher_routes.py:233`

**Issue:** When the publisher endpoint receives a skill that doesn't yet exist, it auto-creates the skill row but doesn't set `creator_id`. The very next request (re-publishing for v1.1.1 etc.) will fail the creator-ownership check because `skill.creator` is None.

**Fix:** When auto-creating a Skill, look up or create the Creator row corresponding to the authenticated user, set `skill.creator_id = creator.id`. Master-key publishes (no user) leave `creator_id = NULL` (admin-owned).

**Test:** Add `test_creator_can_republish_their_own_new_skill` — creator publishes v1.0.0, then v1.0.1, second call must succeed (currently 403).

### F-API-04 [MEDIUM] sandbox runner blocks on `proc.stdout.readline()`

**File:** `app/sandbox/runner.py:418`

**Issue:** `_start_domain_proxy_sync` does `proc.stdout.readline()` to wait for port emission. If the proxy hangs, sandbox execution hangs forever.

**Fix:** Replace with a select-based read loop with a 5-second timeout, capturing stderr too:
```python
import select
deadline = time.monotonic() + 5.0
port_line = None
while time.monotonic() < deadline:
    rl, _, _ = select.select([proc.stdout, proc.stderr], [], [], 0.5)
    if proc.stdout in rl:
        port_line = proc.stdout.readline().rstrip("\n")
        if port_line:
            break
    if proc.stderr in rl:
        err = proc.stderr.readline()
        # log but keep reading stdout
if not port_line:
    proc.terminate()
    proc.wait(timeout=2)
    raise SandboxError(f"proxy did not emit port within 5s; stderr={proc.stderr.read()!r}")
```

**Test:** Add `test_proxy_hang_times_out` — mock the proxy script to sleep forever, expect SandboxError within 6 seconds.

### F-API-05 [MEDIUM] Redis connection thrash

**File:** `app/middleware.py:50` (function `get_redis`)

**Issue:** When Redis is down, every request retries the connection (latency + log spam).

**Fix:** Apply Copilot's suggested patch verbatim — adds `_redis_next_retry_at` with 30s backoff.

**Test:** Add `test_redis_unavailable_does_not_thrash` — mock `redis.from_url` to raise ConnectionError, call `get_redis()` 10 times in 100ms, assert only 1 actual connection attempt.

### F-API-06 [MEDIUM] Env var name mismatch for skills dir

**File:** `app/publisher_routes.py:51` (function `_skills_dir`)

**Issue:** Reads `RECIPES_SKILLS_DIR` directly from `os.environ`, but Settings uses `env_prefix="WR_"`. Setting `WR_RECIPES_SKILLS_DIR` (the natural prod config) is silently ignored.

**Fix:** Use `getattr(settings, "RECIPES_SKILLS_DIR", None) or os.environ.get("RECIPES_SKILLS_DIR") or "/var/lib/recipes-skills"`. Settings already has the field (verify in `app/config.py` and add if missing).

**Test:** Add `test_skills_dir_uses_settings` — set `WR_RECIPES_SKILLS_DIR` env, assert `_skills_dir()` reads it.

### F-API-07 [LOW] VERSION drift

**File:** `app/routes.py:56`

**Issue:** `VERSION = "0.3.0"` while `main.py` reports `0.4.0`.

**Fix:** Bump to `0.4.0` (we just shipped publisher + scanner; this IS the version bump).

**Test:** `curl recipes.wisechef.ai/api/healthz` should return `version: "0.4.0"` after deploy.

### F-API-08 [LOW] Install events missing `version_semver`

**File:** `app/routes.py:231`

**Fix:** Apply Copilot's suggested patch verbatim. Add `version_semver=latest.semver` to the `InstallEvent(...)` call. Verify `models.InstallEvent` has the field; if not, add it (migration).

**Test:** Add `test_install_event_records_version` — install a skill, query `install_events`, assert `version_semver` is populated.

### F-API-09 [LOW] Stripe transfer return type

**File:** `app/stripe_service.py:126`

**Fix:** Change annotation `def create_transfer(...) -> stripe.Transfer:` to `-> stripe.Transfer | None:`. Update one caller (`payout_engine.py`) to handle `None` return.

**Test:** Add `test_create_transfer_below_min_returns_none`.

### F-API-10 [LOW] Sandbox tests assert wrong arg format

**File:** `tests/test_sandbox.py:503, 515`

**Fix:** Tests expect `args.index("--private")` (separate token), but generator emits `--private=<dir>` (single token with `=`). Change test assertions to use `any(a.startswith("--private=") for a in args)`. Apply same pattern for `--timeout=` and `--rlimit-as=`.

**Test:** Tests must pass after the fix.

### F-API-11 [LOW] Mutable default on Pydantic model

**File:** `app/sandbox/routes.py:63, 121`

**Fix:** Change `validation_warnings: list[str] = []` to `validation_warnings: list[str] = Field(default_factory=list)`. Same for the `Body(...)` default at line 121.

**Test:** Add `test_sandbox_run_request_no_shared_state` — instantiate two SandboxRunRequest, modify one's list, assert the other is unaffected.

### F-API-12 [BUG from S3-C stress test] Prompt-injection false-positive

**File:** `app/security_scan.py` (the `prompt_injection` regex)

**Issue:** The regex `ignore\s+(?:all\s+)?previous\s+(?:instructions|context)` matches as a substring regardless of preceding `do not`. Real legitimate text like "do not ignore previous instructions if customer asks for X" gets flagged.

**Fix:** Add a negation-aware lookbehind. Use a regex with negative lookbehind for `(don't|do not|never|cannot|can't|won't|will not)\s+(?:[a-z]+\s+){0,3}` immediately preceding the match, OR (simpler) shift to a 2-pass scan where we first detect the literal phrase and then check the preceding 30 chars for negation markers.

Concrete implementation:
```python
def _check_prompt_injection(self, text: str, file_path: str) -> list[Finding]:
    PI_PATTERNS = [
        re.compile(r"ignore\s+(?:all\s+)?previous\s+(?:instructions|context)", re.I),
        re.compile(r"disregard\s+the\s+(?:system\s+)?prompt", re.I),
        re.compile(r"you\s+are\s+now\s+(?:[A-Z]|a\s+different)", re.I),
        re.compile(r"forget\s+everything\s+(?:above|prior)", re.I),
    ]
    NEGATION_RE = re.compile(
        r"(?:do\s+not|don'?t|never|cannot|can'?t|won'?t|will\s+not)\s+(?:\S+\s+){0,3}$",
        re.I,
    )
    findings = []
    for line_no, line in enumerate(text.splitlines(), 1):
        for pattern in PI_PATTERNS:
            for m in pattern.finditer(line):
                preceding = line[:m.start()]
                if NEGATION_RE.search(preceding):
                    continue  # legitimate negation, skip
                findings.append(Finding(...))
    return findings
```

**Test:** Add `test_prompt_injection_negation_does_not_false_positive` covering:
- "do not ignore previous instructions" → no finding
- "don't ignore previous instructions" → no finding
- "never ignore previous instructions" → no finding
- "ignore previous instructions" → finding (positive case still works)
- "ignore all previous instructions" → finding

### F-API-13 [DEFERRED] sandbox `validation_warnings: list[str] = []` (line 63)

**Same as F-API-11.** Already covered.

---

## Subagent F-CLI — recipes-skill fixes

### F-CLI-01 [LOW] Test asserts wrong wire format

**File:** `tests/test_recipes_cli.py:488`

**Issue:** Assertions check for `sha256`, `public_key`, `version`, `name` form fields — but the CLI now sends `skill_toml`, `tarball`, `signature`, `signing_pubkey` as multipart files plus `is_public` as a form field (per the contract alignment in commit `cdf5c80`).

**Fix:** Update the test assertions to match the actual wire format. Specifically:
- Assert request has multipart parts named `skill_toml`, `tarball`, `signature`, `signing_pubkey`
- Assert form field `is_public` is present
- Remove assertions for `name`, `version`, `sha256`, `public_key` (no longer in the wire)

**Test:** Test passes after rewrite.

### F-CLI-02 [LOW] SUBAGENT_B_OUTPUT.md describes old wire format

**File:** `SPRINT_DOCS/SUBAGENT_B_OUTPUT.md:103`

**Fix:** Apply Copilot's suggested replacement verbatim — describes the actual current wire format with the multipart fields table.

**Test:** N/A (doc).

### F-CLI-03 [BUG from S3-A] Install path defaults to `general/`

**File:** `bin/recipes` (the `cmd_install` function)

**Issue:** `manifest.category` is not populated by the install endpoint, so the CLI defaults to `category = "general"`. Real customers (and our own dogfood) had to manually move from `general/` to `devops/`.

**Fix (CLI side):** Read `category` from skill.toml in the downloaded tarball BEFORE choosing the install path. Order of preference:
1. `manifest.category` from install response (server-side preference, future-proof)
2. `[skill].category` from the downloaded skill.toml (extracted-and-read)
3. fallback `general/`

**Test:** Add `test_install_uses_skill_toml_category` — mock install of a skill with category=devops, assert installed at `~/.hermes/skills/devops/<slug>/`.

**Note:** Server-side fix (returning category in manifest) is a separate concern in recipes-api — handled there in F-API-14 (added below).

---

## Bonus — F-API-14 [from F-CLI-03 server side]

### F-API-14 Install endpoint should return `manifest.category`

**File:** `app/routes.py` (the `install_skill` function)

**Fix:** When building the InstallResponse, populate `manifest` field with the skill's category + tags. Read these from the most recent skill_versions row's parsed skill_toml (already stored in `skill_versions.skill_toml` column).

```python
import tomllib
toml_text = latest.skill_toml or ""
try:
    toml_data = tomllib.loads(toml_text).get("skill", {})
    manifest = {
        "category": toml_data.get("category") or skill.category,
        "tags": toml_data.get("tags", []),
        "tier": toml_data.get("tier"),
    }
except Exception:
    manifest = {"category": skill.category}
```

**Test:** Add `test_install_response_includes_manifest_category`.

---

## Mandatory rules (apply to both subagents)

1. **All fixes go on the existing branches** — `agent/tori/recipes-publisher-sprint2` for F-API-* and `agent/tori/recipes-cli-sprint2` for F-CLI-*. **Do not create new branches.** This way the existing PRs auto-update with the fixes.
2. **Every fix ships with a test that demonstrates the bug being fixed.** Test must FAIL on the unfixed code and PASS after the fix.
3. **Commit each fix as a separate commit** with the message format `fix(<area>): <one-liner> (PR #1 review)` so the PR shows clean history per Copilot comment.
4. **Run the full test suite after each fix** — `PYTHONPATH=. pytest tests/` for recipes-api, `pytest tests/` for recipes-skill. Halt and report if any fix breaks an unrelated test.
5. **Write `SPRINT_DOCS/SUBAGENT_FIX_<TAG>_OUTPUT.md`** before returning, summarizing every fix with: file, before/after, test name added, test result.

## Acceptance gate (Tori main, Turn 3)

- All recipes-api tests pass after fixes (target: 56 → 65+ tests)
- All recipes-skill tests pass after fixes (target: 10 → 12+ tests)
- Re-deploy publisher to prod, re-run S3-C subset (just the false-positive regression: "do not ignore previous instructions" must accept now)
- Re-fetch Copilot review on the updated PRs — should show no new comments OR only `RESOLVED` markers on prior comments
- Merge to main when both PRs show no unresolved review comments
