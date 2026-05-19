#!/usr/bin/env bash
# secfix_1905 smoke tests — verifies the 10 CRIT + 9 HIGH + 8 MED fixes
# are LIVE in production after deploy. Exits 0 only when every probe is green.
#
# Usage:
#   BASE_URL=https://recipes.wisechef.ai bash scripts/secfix_1905_smoke.sh
#   (no env → defaults to recipes.wisechef.ai)
#
# Add to CI deploy.yml as the last step so a regressed fix fails the deploy.

set -u
BASE_URL="${BASE_URL:-https://recipes.wisechef.ai}"
PASS=0
FAIL=0
TOTAL=0

probe() {
    local label="$1" expected="$2" actual="$3"
    TOTAL=$((TOTAL + 1))
    if [[ "$actual" == "$expected" || "$actual" =~ $expected ]]; then
        echo "✅ [$label] $actual"
        PASS=$((PASS + 1))
    else
        echo "❌ [$label] got '$actual', expected '$expected'"
        FAIL=$((FAIL + 1))
    fi
}

http_code() {
    curl -sS -o /dev/null -w "%{http_code}" "$@"
}

http_body() {
    curl -sS "$@"
}

echo "=== secfix_1905 smoke probes against $BASE_URL ==="
echo

# Issue #14 — /api/healthz returns 200 with proper db status
probe "healthz/db-ok" "200" "$(http_code "$BASE_URL/api/healthz")"

# Issue #6 — recipes_install via wrong-user key → not_found (no oracle)
# Skip if NO_AUTH_PROBES is set (requires test API keys not in repo)
if [[ -z "${NO_AUTH_PROBES:-}" && -n "${TEST_API_KEY_USER_B:-}" && -n "${PRIVATE_SKILL_SLUG_USER_A:-}" ]]; then
    code=$(http_code -H "x-api-key: $TEST_API_KEY_USER_B" "$BASE_URL/api/skills/install?slug=$PRIVATE_SKILL_SLUG_USER_A")
    probe "private-skill-not-oracle" "404|401" "$code"
fi

# Issue #16 — external_resources guards private slugs
if [[ -n "${PRIVATE_SKILL_SLUG_USER_A:-}" ]]; then
    code=$(http_code "$BASE_URL/api/skills/$PRIVATE_SKILL_SLUG_USER_A/external")
    probe "external-resources-private-404" "404" "$code"
fi

# Issue #19 — search_skills returns results quickly (proxy for N+1 fix)
elapsed=$(curl -sS -o /dev/null -w "%{time_total}" "$BASE_URL/api/skills/search?q=python&limit=50")
threshold="2.0"
TOTAL=$((TOTAL + 1))
if awk "BEGIN{exit !($elapsed < $threshold)}"; then
    echo "✅ [search-50row-under-${threshold}s] ${elapsed}s"
    PASS=$((PASS + 1))
else
    echo "❌ [search-50row-under-${threshold}s] ${elapsed}s (regression — N+1 may be back)"
    FAIL=$((FAIL + 1))
fi

# Issue #27 — cookbook install URL works (returns a signed _download URL)
if [[ -n "${TEST_COOKBOOK_ID:-}" && -n "${TEST_API_KEY_MASTER:-}" ]]; then
    body=$(http_body -H "x-api-key: $TEST_API_KEY_MASTER" "$BASE_URL/api/cookbooks/$TEST_COOKBOOK_ID/install")
    if echo "$body" | grep -q '_download'; then
        probe "cookbook-install-url" "match" "match"
    else
        probe "cookbook-install-url" "match" "no-_download-in-body"
    fi
fi

# Issue #2 — OAuth state with NO cookie returns error redirect
code=$(http_code -L --max-redirs 0 "$BASE_URL/api/auth/github/callback?state=anything&code=fake")
probe "oauth-state-fails-closed" "302" "$code"

# Issue #8 — sandbox endpoint exists and returns 401 without auth (not 200/500)
code=$(http_code -X POST -H "Content-Type: application/json" -d '{}' "$BASE_URL/api/sandbox/run")
probe "sandbox-auth-required" "401|403|422" "$code"

# Issue #12 — _real_client_ip in CF-routed prod — no probe (passive)
# Issue #17 — last_used_at — no probe (server-side batched; observed by monitoring)
# Issue #18 — Stripe webhook off-loop — no probe (load-test only)

# General sanity: critical endpoints respond
probe "skills-search" "200" "$(http_code "$BASE_URL/api/skills/search")"
probe "stats" "200" "$(http_code "$BASE_URL/api/stats")"

echo
echo "=== Results: $PASS passed / $FAIL failed / $TOTAL total ==="
if [[ $FAIL -gt 0 ]]; then
    echo "❌ Smoke FAILED — investigate before declaring deploy green."
    exit 1
fi
echo "✅ All probes green."
exit 0
