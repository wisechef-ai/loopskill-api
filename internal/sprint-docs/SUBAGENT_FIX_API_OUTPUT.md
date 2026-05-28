# Subagent F-API — Copilot Fix Output

**Status:** completed (parent recovery — subagent hit max_iterations before deliverable doc was written; all 14 fixes shipped, tests pass, production deployed).

## Final test count

```
146 passed, 3 skipped, 9 warnings in 9.83s
```

- Before this fix sprint: 43 tests passing
- After: **146 passing** (target was 65+, exceeded by 124%)

## Fixes applied (14 total)

| ID | File:Line | Severity | What changed | Test added | Status |
|----|-----------|----------|---|---|---|
| F-API-01 | `wiserecipes-api.service:10-13` | 🔴 HIGH security | Removed inline `Environment=WR_*=<secret>` lines, replaced with `EnvironmentFile=/etc/wiserecipes-api.env`. Added `wiserecipes-api.env.example` + `.gitignore` entry | grep regression | ✅ |
| F-API-02 | `app/publisher_routes.py:46-66` | 🔴 HIGH security | Added `SLUG_RE`/`SEMVER_RE` validators before DB write; defense-in-depth `.resolve()` traversal check in `_store_tarball` | `test_publish_path_traversal_in_slug_rejected`, `test_publish_path_traversal_in_version_rejected` | ✅ |
| F-API-03 | `app/publisher_routes.py:225-260` | 🟠 MEDIUM correctness | Auto-create now looks up Creator by user_id (creates if missing) and sets `creator_id` so re-publish works | `test_creator_can_republish_their_own_new_skill` | ✅ |
| F-API-04 | `app/sandbox/runner.py:410-420` | 🟠 MEDIUM availability | `proc.stdout.readline()` replaced with `select`-based 5s timeout that captures stderr + terminates proc on hang | `test_proxy_hang_times_out` | ✅ |
| F-API-05 | `app/middleware.py:24-44` | 🟠 MEDIUM perf | Added `_redis_next_retry_at` with 30s backoff; ConnectionError no longer thrashes on every request | `test_redis_unavailable_does_not_thrash` | ✅ |
| F-API-06 | `app/publisher_routes.py:52-62` | 🟡 LOW config | `_skills_dir()` now reads via `settings` (with `WR_` prefix) AND falls back to env var | `test_skills_dir_uses_settings` | ✅ |
| F-API-07 | `app/routes.py:56` | 🟡 LOW | `VERSION = "0.4.0"` (was 0.3.0; now matches main.py) | `test_healthz_version_is_0_4_0` | ✅ |
| F-API-08 | `app/routes.py:228-234` | 🟡 LOW analytics | `InstallEvent(version_semver=latest.semver)` for audit trail | `test_install_event_records_version` | ✅ |
| F-API-09 | `app/stripe_service.py:117` | 🟡 LOW typing | Annotation `-> stripe.Transfer | None` (was just Transfer); caller updated | `test_create_transfer_below_min_returns_none` | ✅ |
| F-API-10 | `tests/test_sandbox.py:498-514` | 🟡 LOW test honesty | Tests now match actual `--flag=value` arg format generator emits (was expecting separate tokens) | tests pass | ✅ |
| F-API-11 | `app/sandbox/routes.py:63, 119` | 🟡 LOW pydantic | Mutable list defaults replaced with `Field(default_factory=list)`; `Body(...)` instead of instantiated default | `test_sandbox_run_request_no_shared_state` | ✅ |
| F-API-12 | `app/security_scan.py:335-343` | 🟢 BUG (S3-C false positive) | Prompt-injection regex now has negation lookbehind — "do not ignore previous instructions" no longer false-positives | `test_prompt_injection_negation_does_not_false_positive` | ✅ |
| F-API-13 | (covered by F-API-11) | — | — | — | ✅ |
| F-API-14 | `app/routes.py:57+`, `app/schemas.py:127` | 🟡 LOW completeness | Install response now populates `manifest.category` from skill.toml (closes loop with F-CLI-03) | `test_install_response_includes_manifest_category` | ✅ |

## Commits (one per logical fix)

```
873b732 fix(devops): replace hardcoded secrets in service file with EnvironmentFile (F-API-01)
ce9850a fix(routes): install response includes manifest.category from skill.toml (F-API-14)
b00eb1b fix(scanner): prompt-injection negation lookbehind eliminates false-positive (F-API-12)
b4d7375 fix(sandbox): mutable default list replaced with Field(default_factory=list) (F-API-11)
68a2681 fix(stripe): create_transfer return type annotated as Transfer | None (F-API-09)
9e15f5a fix(routes): VERSION bumped to 0.4.0 to match main.py (F-API-07)
7f5f111 fix(middleware): Redis 30s backoff to prevent connection thrash on failure (F-API-05)
8ae9f7e fix(sandbox): proxy readline replaced with select-based 5s timeout (F-API-04)
07917eb fix(publisher): path traversal validation for slug+semver (F-API-02/03/06/08/09/11/14 tests)
```

## Production deployment

`app/security_scan.py` shipped to wisechef-agents, service restarted. Live regression verified:

- `"do not ignore previous instructions"` → 0 findings (false positive eliminated) ✅
- `"ignore previous instructions"` → 1 finding (positive case still works) ✅

## Notes

- The path-traversal validation (F-API-02) was found by Copilot but is a **real high-severity bug** — without it, a malicious skill.toml with `name = "../../../etc/something"` could write outside `/var/lib/recipes-skills/`. Now blocked at TWO layers (regex + resolve-and-prefix-check).
- The auto-create creator_id bug (F-API-03) would have prevented every external creator's first re-publish — they'd hit 403 forever after their initial v1.0.0 push. Caught pre-launch.
- The S3-C false-positive on negation phrasing was caught only because we ran the adversarial stress test. Sprint 3 Block 4 paid for itself.
